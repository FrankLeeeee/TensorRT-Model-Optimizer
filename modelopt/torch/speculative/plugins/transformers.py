# Adapted from: https://github.com/ctlllll/axolotl/blob/f86767e/src/axolotl/monkeypatch/medusa_utils.py
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Support speculative decoding for huggingface models."""

import contextlib
from typing import Any

import torch
from torch import nn
from torch.nn import CrossEntropyLoss
from transformers import Cache, DynamicCache, PreTrainedModel
from transformers.trainer_pt_utils import LabelSmoother
from transformers.utils import ModelOutput

from ..eagle.conversion import EagleDMRegistry
from ..eagle.eagle_model import EagleModel
from ..eagle.utils import RMSNorm, expand_mask, make_causal_mask
from ..medusa.conversion import MedusaDMRegistry
from ..medusa.medusa_model import MedusaModel
from ..utils import ResBlock

IGNORE_TOKEN_ID = LabelSmoother.ignore_index


@MedusaDMRegistry.register({PreTrainedModel: "hf.PreTrainedModel"})
class HFMedusaModel(MedusaModel):
    """Medusa Model Class for huggingface models."""

    def modify(self, medusa_num_heads=0, medusa_num_layers=0):
        """Constructor.

        Args:
            medusa_num_heads: number of medusa heads.
            medusa_num_layers: number of ResBlock layers in each head.
        """
        super().modify(medusa_num_heads=medusa_num_heads, medusa_num_layers=medusa_num_layers)
        self.config.medusa = {
            "num_medusa_heads": medusa_num_heads,
            "num_medusa_layers": medusa_num_layers,
        }

        hidden_size = self.lm_head.weight.shape[-1]
        vocab_size = self.lm_head.weight.shape[0]

        # Create a list of Medusa heads
        self.medusa_heads = nn.ModuleList(
            [
                nn.Sequential(
                    *([ResBlock(hidden_size)] * self.medusa_num_layers),
                    nn.Linear(hidden_size, vocab_size, bias=False),
                )
                for _ in range(self.medusa_num_heads)
            ]
        )

        # Ensure medusa_head's dtype and device align with the base_model
        self.medusa_heads.to(self.lm_head.weight.dtype).to(self.lm_head.weight.device)
        self.medusa_heads.device = self.lm_head.weight.device
        if hasattr(self, "hf_device_map") and "lm_head" in self.hf_device_map:
            self.hf_device_map["medusa_heads"] = self.hf_device_map["lm_head"]

    def forward(
        self,
        input_ids: torch.LongTensor | None = None,
        attention_mask: torch.Tensor | None = None,
        position_ids: torch.LongTensor | None = None,
        past_key_values: Cache | None = None,
        inputs_embeds: torch.FloatTensor | None = None,
        labels: torch.LongTensor | None = None,
        use_cache: bool | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
        cache_position: torch.LongTensor | None = None,
        logits_to_keep: int | torch.Tensor = 0,
        freeze_base_model: bool = True,
        medusa_heads_coefficient: float | None = 0.2,
        medusa_decay_coefficient: float | None = 0.8,
        **kwargs,
    ) -> Any:
        """Forward pass of the MedusaModel.

        Returns:
            torch.Tensor: A tensor containing predictions from all Medusa heads.
        """
        # Pass input through the base model
        with torch.no_grad() if freeze_base_model else contextlib.nullcontext():
            outputs = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                rcache_position=cache_position,
                **kwargs,
            )
            hidden_states = outputs.last_hidden_state
            # Only compute necessary logits, and do not upcast them to float if we are not computing the loss
            slice_indices = (
                slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
            )
            logits = self.lm_head(hidden_states[:, slice_indices, :])

        medusa_logits = [
            self.medusa_heads[i](hidden_states[:, slice_indices, :])
            for i in range(self.medusa_num_heads)
        ]

        if labels is not None:
            loss = 0
            loss_fct = CrossEntropyLoss()
            # Base model loss
            if not freeze_base_model:
                loss_logits = logits.view(-1, logits.shape[-1])
                loss_labels = labels.view(-1)
                base_model_loss = loss_fct(loss_logits, loss_labels)
                loss += base_model_loss
            # Medusa loss
            for i in range(self.medusa_num_heads):
                labels = labels[..., 1:].contiguous()
                loss_logits = medusa_logits[i][:, : -(1 + i)].contiguous()
                loss_logits = loss_logits.view(-1, loss_logits.shape[-1])
                loss_labels = labels.view(-1)
                loss += (
                    loss_fct(loss_logits, loss_labels)
                    * medusa_decay_coefficient**i
                    * medusa_heads_coefficient
                )
        else:
            loss = None

        return ModelOutput(
            loss=loss,
            logits=logits,
            medusa_logits=medusa_logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


class EagleModule(nn.Module):
    """Eagle module used in EAGLE model."""

    def __init__(self, config, decoder_layer_cls, num_layers, use_last_layernorm=False, bias=True):
        """Init function for EagleModule."""
        super().__init__()
        self.fc = nn.Linear(3 * config.hidden_size, config.hidden_size, bias=bias)
        self.layers = nn.ModuleList(
            [decoder_layer_cls(config, layer_idx) for layer_idx in range(num_layers)]
        )
        if use_last_layernorm:
            self.norm = RMSNorm(config.hidden_size, config.rms_norm_eps)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

    def _prepare_tree_attention_mask(
        self, attention_mask, seq_lens, past_key_values_length
    ):
        """
        准备树形attention mask：
        - 保留原有的方形attention_mask
        - 根据past_key_values_length，在后面扩展对角mask块
        
        Args:
            attention_mask: 原有的方形mask [bsz, 1, seq_len, seq_len] 
            seq_lens: 当前序列长度
            past_key_values_length: 缓存的总长度
        
        Returns:
            扩展后的attention mask [bsz, 1, seq_len, seq_len + past_key_values_length]
        """
        if attention_mask is None:
            return None
            
        bsz, num_heads, tgt_len, src_len = attention_mask.shape
        device = attention_mask.device
        dtype = attention_mask.dtype
        
        if past_key_values_length == 0:
            return attention_mask
        
        # 扩展后的总长度
        extended_src_len = src_len + past_key_values_length
        
        # 创建扩展后的mask矩阵
        extended_mask = torch.full(
            (bsz, num_heads, tgt_len, extended_src_len),
            torch.finfo(dtype).min,  # 默认屏蔽
            device=device,
            dtype=dtype
        )
        
        # 1. 复制原有的方形mask
        extended_mask[:, :, :, :src_len] = attention_mask
        
        # 2. 添加对角mask块 - 每个块都是完整的对角线
        num_diagonal_blocks = past_key_values_length // seq_lens
        
        for block_idx in range(num_diagonal_blocks):
            block_start = src_len + block_idx * seq_lens
            
            # 为每个块创建对角线模式
            for i in range(min(tgt_len, seq_lens)):
                col_idx = block_start + i
                if col_idx < extended_src_len:
                    extended_mask[:, :, i, col_idx] = 0.0  # 允许attention
        return extended_mask
        

    def forward(
        self,
        hidden_states: torch.Tensor,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor | None = None,
        loss_mask: torch.Tensor | None = None,
        logits: torch.Tensor | None = None,
        position_ids: torch.LongTensor | None = None,
        past_key_values: Cache | None = None,
        use_cache: bool | None = None,
        output_attentions: bool | None = False,
        position_embeddings: torch.Tensor | None = None,
    ):
        """Forward function for EagleModule."""

        inputs_embeds = inputs_embeds.to(hidden_states.dtype).to(hidden_states.device)
        proj_hidden_states = hidden_states

        attention_mask = self._prepare_tree_attention_mask(
            attention_mask, hidden_states.shape[1], past_key_values.get_seq_length()
        )

        for idx, decoder_layer in enumerate(self.layers):
            layer_outputs = decoder_layer(
                hidden_states,
                input_embeds=inputs_embeds,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_values,
                output_attentions=output_attentions,
                use_cache=use_cache,
                position_embeddings=position_embeddings,
            )

            hidden_states = layer_outputs[0]

        if hasattr(self, "norm"):
            hidden_states = self.norm(hidden_states)

        logits = self.lm_head(hidden_states).to(hidden_states.device)

        return proj_hidden_states, hidden_states, logits, past_key_values


from transformers.models.llama.modeling_llama import LlamaAttention, LlamaRMSNorm, LlamaDecoderLayer
from typing import Optional, Tuple, Unpack, Callable
from torch import nn
from torch.nn import CrossEntropyLoss
from transformers import Cache, DynamicCache, PreTrainedModel
from transformers.trainer_pt_utils import LabelSmoother
from transformers.utils import ModelOutput
from transformers.models.llama.modeling_llama import FlashAttentionKwargs, apply_rotary_pos_emb, repeat_kv

def eager_attention_forward(
    module: nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    scaling: float,
    dropout: float = 0.0,
    **kwargs,
):
    key_states = repeat_kv(key, module.num_key_value_groups)
    value_states = repeat_kv(value, module.num_key_value_groups)

    attn_weights = torch.matmul(query, key_states.transpose(2, 3)) * scaling
    if attention_mask is not None:
        # use tree mask here
        # print(attn_weights.shape, attention_mask.shape)
        attn_weights = attn_weights + attention_mask

    attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query.dtype)
    attn_weights = nn.functional.dropout(attn_weights, p=dropout, training=module.training)
    attn_output = torch.matmul(attn_weights, value_states)
    attn_output = attn_output.transpose(1, 2).contiguous()

    return attn_output, attn_weights

class ModifiedLlamaAttention(LlamaAttention):
    def __init__(self, config, layer_idx):
        super().__init__(config, layer_idx)
        self.q_proj = nn.Linear(config.hidden_size * 2, config.num_attention_heads * config.head_dim, bias=config.attention_bias)
        self.k_proj = nn.Linear(config.hidden_size * 2, config.num_key_value_heads * config.head_dim, bias=config.attention_bias)
        self.v_proj = nn.Linear(config.hidden_size * 2 , config.num_key_value_heads * config.head_dim, bias=config.attention_bias)
        self.o_proj = nn.Linear(config.num_attention_heads * config.head_dim, config.hidden_size, bias=config.attention_bias)
        
    def forward(
        self,
        hidden_states: torch.Tensor,
        position_embeddings: Tuple[torch.Tensor, torch.Tensor],
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        **kwargs: Unpack[FlashAttentionKwargs],
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        input_shape = hidden_states.shape[:-1]
        hidden_shape = (*input_shape, -1, self.head_dim)

        query_states = self.q_proj(hidden_states).view(hidden_shape).transpose(1, 2)
        key_states = self.k_proj(hidden_states).view(hidden_shape).transpose(1, 2)
        value_states = self.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)

        cos, sin = position_embeddings
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin)

        if past_key_value is not None:
            # sin and cos are specific to RoPE models; cache_position needed for the static cache
            cache_kwargs = {"sin": sin, "cos": cos, "cache_position": cache_position}
            key_states, value_states = past_key_value.update(key_states, value_states, self.layer_idx, cache_kwargs)

        attention_interface: Callable = eager_attention_forward

        attn_output, attn_weights = attention_interface(
            self,
            query_states,
            key_states,
            value_states,
            attention_mask,
            dropout=0.0 if not self.training else self.attention_dropout,
            scaling=self.scaling,
            **kwargs,
        )

        attn_output = attn_output.reshape(*input_shape, -1).contiguous()
        attn_output = self.o_proj(attn_output)
        return attn_output, attn_weights

class ModifiedLlamaDecoderLayer(LlamaDecoderLayer):
    def __init__(self, config, layer_idx):
        super().__init__(config, layer_idx)
        self.self_attn = ModifiedLlamaAttention(config, layer_idx)
        self.hidden_norm = LlamaRMSNorm(config.hidden_size, config.rms_norm_eps)

    def forward(
        self,
        hidden_states: torch.Tensor,
        input_embeds: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # necessary, but kept here for BC
        **kwargs: Unpack[FlashAttentionKwargs],
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        residual = hidden_states

        embeds = self.input_layernorm(input_embeds)
        hidden_states = self.hidden_norm(hidden_states)
        hidden_states = torch.cat([embeds, hidden_states], dim=-1)

        # Self Attention
        hidden_states, self_attn_weights = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            cache_position=cache_position,
            position_embeddings=position_embeddings,
            **kwargs,
        )
        # hidden_states = residual + hidden_states

        # Fully Connected
        # residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        # hidden_states = residual + hidden_states

        outputs = (hidden_states,)
        if output_attentions:
            outputs += (self_attn_weights,)

        return outputs

    


@EagleDMRegistry.register({PreTrainedModel: "hf.PreTrainedModel"})
class HFEagleModel(EagleModel):
    """Eagle Model Class for huggingface models."""

    def _set_default_aux_hidden_state_layers(self):
        num_layers = self.config.num_hidden_layers
        self.eagle_aux_hidden_state_layer_ids = [1, num_layers // 2 - 1, num_layers - 4]

    def modify(
        self,
        eagle_num_layers,
        use_input_layernorm_in_first_layer,
        use_last_layernorm,
        eagle_hidden_state_distillation,
        use_aux_hidden_state,
        eagle_aux_hidden_state_layer_ids,
        eagle_disable_moe,  # Not used in HFEagleModel
        draft_vocab_size,
        use_mtp_layernorm,
        ffn_hidden_size=0,
    ):
        """Constructor.

        Args:
            config: The config for eagle decoder layers.
        """
        super().modify(
            eagle_num_layers=eagle_num_layers,
            use_input_layernorm_in_first_layer=use_input_layernorm_in_first_layer,
            use_last_layernorm=use_last_layernorm,
            eagle_hidden_state_distillation=eagle_hidden_state_distillation,
            use_aux_hidden_state=use_aux_hidden_state,
            eagle_aux_hidden_state_layer_ids=eagle_aux_hidden_state_layer_ids,
            eagle_disable_moe=eagle_disable_moe,
            draft_vocab_size=draft_vocab_size,
            use_mtp_layernorm=use_mtp_layernorm,
        )

        from copy import deepcopy
        eagle_config = deepcopy(self.config)
        # update the config
        eagle_config.num_hidden_layers = eagle_num_layers
        eagle_config.use_input_layernorm_in_first_layer = use_input_layernorm_in_first_layer
        eagle_config.use_last_layernorm = use_last_layernorm

        self.config.eagle = eagle_config
        # type(self.model.layers[-1])
        self.eagle_module = EagleModule(
            self.config.eagle, ModifiedLlamaDecoderLayer, eagle_num_layers, use_last_layernorm
        )

        if hasattr(self.model.layers[-1].self_attn, "o_proj"):
            device = self.model.layers[-1].self_attn.o_proj.weight.device
        elif hasattr(self.model.layers[-1].self_attn, "q_proj"):
            device = self.model.layers[-1].self_attn.q_proj.weight.device
        elif hasattr(self.model.layers[-1].self_attn, "qkv_proj"):
            device = self.model.layers[-1].self_attn.qkv_proj.weight.device
        self.eagle_module.to(self.dtype).to(device)

        # Make sure self.model.embed_tokens and self.lm_head are frozen
        for param in self.model.embed_tokens.parameters():
            param.requires_grad = False
        for param in self.lm_head.parameters():
            param.requires_grad = False

    def _prepare_decoder_attention_mask(
        self, attention_mask, input_shape, inputs_embeds, past_key_values_length
    ):
        # create causal mask
        # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
        combined_attention_mask = None
        if input_shape[-1] > 1:
            combined_attention_mask = make_causal_mask(
                input_shape,
                inputs_embeds.dtype,
                device=inputs_embeds.device,
                past_key_values_length=past_key_values_length,
            )

        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            expanded_attn_mask = expand_mask(
                attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]
            ).to(inputs_embeds.device)
            combined_attention_mask = (
                expanded_attn_mask
                if combined_attention_mask is None
                else expanded_attn_mask + combined_attention_mask
            )

        return combined_attention_mask

    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: torch.Tensor | None = None,
        position_ids: torch.LongTensor | None = None,
        past_key_values: Cache | None = None,
        inputs_embeds: torch.FloatTensor | None = None,
        labels: torch.LongTensor | None = None,
        use_cache: bool | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
        cache_position: torch.LongTensor | None = None,
        logits_to_keep: int = 0,
        loss_mask: torch.Tensor | None = None,
        freeze_base_model: bool = True,
        classification_loss_coefficient: float | None = 0.1,
        regression_loss_coefficient: float | None = 1,
        **kwargs
    ) -> Any:
        """Forward pass of the EagleModel.

        Returns:
            hidden_states: The hidden state from the base model.
            logits: logits from the base model.
            eagle_hidden_states: The hidden state from eagle_module.
            eagle_logits: logits from the eagle_module.
        """
        eagle_cache = DynamicCache()
        
        with torch.no_grad() if freeze_base_model else contextlib.nullcontext():
            outputs = super().forward(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                labels=None,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=True,
                cache_position=cache_position,
                logits_to_keep=logits_to_keep,
                **kwargs,
            )
            past_key_values = outputs.past_key_values
            if not isinstance(past_key_values, Cache):
                past_key_values = DynamicCache.from_legacy_cache(past_key_values)

            if self.use_aux_hidden_state:
                aux_hidden_states = [
                    outputs.hidden_states[layer_id] for layer_id in self.eagle_aux_hidden_state_layer_ids
                ]
                hidden_states = torch.cat(aux_hidden_states, dim=-1)
            else:
                hidden_states = outputs.hidden_states[-1]
            logits = outputs.logits

            target_hidden_states = outputs.hidden_states[-1]
        
        # Shift left 1 token for eagle inputs
        loss_list = []
        loss_weight = [0.8 ** i for i in range(4)]
        
        # use fc to make 3 * hidden -> 1 * hidden
        hidden_states = self.eagle_module.fc(hidden_states)
        
        # start up attention mask / embedding
        batch_size, seq_length, _ = hidden_states.shape
        seq_length_with_past = seq_length
        
        device = hidden_states.device
        past_key_values_length = 0
        if position_ids is None:
            position_ids = torch.arange(
                past_key_values_length,
                seq_length + past_key_values_length,
                dtype=torch.long,
                device=device,
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()
        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.bool, device=hidden_states.device
            )
        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, (batch_size, seq_length), hidden_states, past_key_values_length
        )
        
        print(attention_mask.shape)
        
        # prepare origin position ids
        # 在循环开始前
        original_position_ids = position_ids.clone()

        for i in range(4):
            if torch.distributed.get_rank() == 1:
                print("ttt_step: ", i)
                print("kv cache len: ", eagle_cache.get_seq_length())

            # update position ids
            cache_length = eagle_cache.get_seq_length()
            current_position_ids = original_position_ids + cache_length
            with torch.no_grad():
                inputs_embeds = self.model.embed_tokens(input_ids)

            position_embeddings = self.model.rotary_emb(hidden_states, current_position_ids)

            _, eagle_hidden_states, eagle_logits, eagle_cache = self.eagle_module(
                hidden_states,
                inputs_embeds,
                attention_mask=attention_mask,
                position_ids=current_position_ids,
                past_key_values=eagle_cache,
                use_cache=True,
                output_attentions=output_attentions,
                position_embeddings=position_embeddings,
            )
            # Not use cache for ttt right now
            # if not isinstance(eagle_cache, Cache):
            #     eagle_cache = DynamicCache.from_legacy_cache(eagle_cache)
            # past_key_values.eagle_cache = eagle_cache

            hidden_states = eagle_hidden_states

            loss = None
            if not freeze_base_model and labels is not None:
                loss_fct = CrossEntropyLoss()
                loss_logits = logits.view(-1, logits.shape[-1])
                labels = labels.view(-1)
                base_model_loss = loss_fct(loss_logits, labels)
                loss = base_model_loss

            if loss_mask is not None:

                regression_loss, classification_loss = self._eagle_loss(
                    target_hidden_states, logits, eagle_hidden_states, eagle_logits, loss_mask
                )
                # use classification loss only for EAGLE-3
                eagle_loss = (
                    classification_loss
                )
                loss_list.append(eagle_loss)
                
            # Padding logic after iteration
            if i < 3:
                # target
                zeropadding = torch.zeros(
                    logits.shape[0],
                    1,
                    logits.shape[2],
                    dtype=hidden_states.dtype,
                    device=hidden_states.device,
                )
                # use base model output for hidden states
                logits = torch.cat((logits[:, 1:], zeropadding), dim=1).detach()
                # input ids
                zeropadding = torch.zeros(
                    input_ids.shape[0], 1, dtype=input_ids.dtype, device=input_ids.device
                )
                input_ids = torch.cat((input_ids[:, 1:], zeropadding), dim=1)
                # Loss Mask
                if loss_mask is not None:
                    zeropadding = torch.zeros(
                        loss_mask.shape[0], 1, dtype=loss_mask.dtype, device=loss_mask.device
                    )
                    loss_mask = torch.cat((loss_mask[:, 1:], zeropadding), dim=1)
                # Attention Mask
                ind = torch.arange(seq_length, device=attention_mask.device)
                ind0 = ind[i:]
                ind1 = ind[:seq_length-i]
                attention_mask[:, :, ind0, ind1] = torch.finfo(attention_mask.dtype).min


        loss = sum([loss_weight[i] * loss_list[i] for i in range(4)])

        return ModelOutput(
            loss=loss,
            logits=logits,
            eagle_logits=eagle_logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def _eagle_loss(self, hidden_states, logits, eagle_hidden_states, eagle_logits, loss_mask):
        """Function for EAGLE loss computing."""
        loss_mask = loss_mask[:, :, None]
        criterion = nn.SmoothL1Loss(reduction="none")
        classification_loss = nn.Softmax(dim=2)(logits) * nn.LogSoftmax(dim=2)(eagle_logits)
        classification_loss = -torch.sum(torch.sum(loss_mask * classification_loss, 2)) / (
            loss_mask.sum() + 1e-5
        )
        regression_loss = criterion(eagle_hidden_states, hidden_states)
        regression_loss = torch.sum(torch.mean(loss_mask * regression_loss, 2)) / (
            loss_mask.sum() + 1e-5
        )
        return regression_loss, classification_loss
    