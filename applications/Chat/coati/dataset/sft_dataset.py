#    Copyright 2023 Rohan Taori, Ishaan Gulrajani, Tianyi Zhang, Yann Dubois, Xuechen Li
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import copy
from typing import Dict, Optional, Sequence, Tuple

import torch
from coati.models.chatglm.chatglm_tokenizer import ChatGLMTokenizer
from torch.utils.data import Dataset
from tqdm import tqdm
from transformers import PreTrainedTokenizer

from colossalai.logging import get_dist_logger

from .utils import is_rank_0, jload, to_tensor

logger = get_dist_logger()

IGNORE_INDEX = -100
PROMPT_DICT = {
    "prompt_input": ("Below is an instruction that describes a task, paired with an input that provides further context. "
                     "Write a response that appropriately completes the request.\n\n"
                     "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"),
    "prompt_no_input": ("Below is an instruction that describes a task. "
                        "Write a response that appropriately completes the request.\n\n"
                        "### Instruction:\n{instruction}\n\n### Response:"),
}


def _preprocess(sources: Sequence[str],
                targets: Sequence[str],
                tokenizer: PreTrainedTokenizer,
                max_length: int,
                ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Preprocess the data by tokenizing."""
    sequences = [s + t for s, t in zip(sources, targets)]
    sequences_token = tokenizer(sequences,
                                max_length=max_length,
                                padding="max_length",
                                truncation=True)
    sequences_token = to_tensor(sequences_token)
    sources_token = tokenizer(sources,
                              max_length=max_length,
                              padding="max_length",
                              truncation=True)
    sources_token = to_tensor(sources_token)

    assert sequences_token["attention_mask"].dim() == 2, \
        "seq2seq model should be preprocessed differently"
    labels = copy.deepcopy(sequences_token["input_ids"])
    for i in range(labels.shape[0]):
        source_len = sources_token["attention_mask"][i].sum().item()
        pad_len = max_length - sequences_token["attention_mask"][i].sum().item()
        if tokenizer.padding_side == "right":
            # |prompt|completion|eos|pad|
            labels[i][:source_len] = IGNORE_INDEX
            labels[i][-pad_len:] = IGNORE_INDEX
        elif tokenizer.padding_side == "left":
            # |pad|prompt|completion|eos|
            labels[i][:pad_len + source_len] = IGNORE_INDEX
        else:
            raise RuntimeError()

    return sequences_token["input_ids"], labels, sequences_token["attention_mask"]


def _preprocess_chatglm(sources: Sequence[str],
                        targets: Sequence[str],
                        tokenizer: PreTrainedTokenizer,
                        max_length: int,
                        ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Preprocess the data by tokenizing."""
    sequences = [s + "[gMASK]" + tokenizer.bos_token + t for s, t in zip(sources, targets)]
    
    sequences_token = tokenizer(sequences,
                                max_length=max_length,
                                padding="max_length",
                                truncation=True)
    sequences_token = to_tensor(sequences_token)

    labels = copy.deepcopy(sequences_token["input_ids"])
    for i in range(labels.shape[0]):
        assert tokenizer.padding_side == "left", "chatglm's tokenizer should be padded at left"
        bos_loc = torch.nonzero(sequences_token["input_ids"][i] == tokenizer.bos_token_id).squeeze()
        context_len = bos_loc[0] if len(bos_loc.shape) > 0 else bos_loc.item()
        labels[i][:context_len] = IGNORE_INDEX

    return sequences_token["input_ids"], labels, sequences_token["attention_mask"]


class SFTDataset(Dataset):
    """
    Dataset for sft model

    Args:
        dataset: dataset for supervised model
        tokenizer: tokenizer for supervised model
        max_length: max length of input
    """

    def __init__(self,
                 dataset: Dict,
                 tokenizer: PreTrainedTokenizer,
                 max_length: int = 512
                 ) -> None:
        super().__init__()
        self.input_ids = []

        sources = [data["prompt"] for data in dataset]
        targets = [
            data["completion"] + tokenizer.eos_token
            for data in tqdm(dataset, disable=not is_rank_0())
        ]
        if isinstance(tokenizer, ChatGLMTokenizer):
            assert tokenizer.padding_side == "left", "ChatGLM's tokenizer should be padded at left"
            self.input_ids, self.labels, self.attention_mask = \
                _preprocess_chatglm(sources, targets, tokenizer, max_length)
        else:
            self.input_ids, self.labels, self.attention_mask = \
                _preprocess(sources, targets, tokenizer, max_length)

    def __len__(self):
        length = self.input_ids.shape[0]
        return length

    def __getitem__(self, idx):
        return dict(input_ids=self.input_ids[idx],
                    labels=self.labels[idx],
                    attention_mask=self.attention_mask[idx])


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self,
                 data_path: str,
                 tokenizer: PreTrainedTokenizer,
                 max_datasets_size: Optional[int] = None,
                 max_length: int = 512):
        super().__init__()
        logger.info("Loading data...")
        list_data_dict = jload(data_path)
        logger.info(f"Loaded {len(list_data_dict)} examples.")

        if max_datasets_size is not None:
            logger.info(f"Limiting dataset to {max_datasets_size} examples.")
            list_data_dict = list_data_dict[:max_datasets_size]

        logger.info("Formatting inputs...")
        prompt_input, prompt_no_input = PROMPT_DICT["prompt_input"], PROMPT_DICT["prompt_no_input"]
        sources = [
            prompt_input.format_map(example) if "input" in example else prompt_no_input.format_map(example)
            for example in list_data_dict
        ]
        targets = [
            example['output'] + tokenizer.eos_token
            for example in list_data_dict
        ]

        logger.info("Tokenizing inputs... This may take some time...")
        if isinstance(tokenizer, ChatGLMTokenizer):
            assert tokenizer.padding_side == "left", "ChatGLM's tokenizer should be padded at left"
            self.input_ids, self.labels, self.attention_mask = \
                _preprocess_chatglm(sources, targets, tokenizer, max_length)
        else:
            self.input_ids, self.labels, self.attention_mask = \
                _preprocess(sources, targets, tokenizer, max_length)

        logger.info("Loaded dataset.")

    def __len__(self):
        length = self.input_ids.shape[0]
        return length

    def __getitem__(self, idx):
        return dict(input_ids=self.input_ids[idx],
                    labels=self.labels[idx],
                    attention_mask=self.attention_mask[idx])
