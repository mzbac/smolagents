#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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
import json
import logging
import os
import random
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from huggingface_hub import InferenceClient
from huggingface_hub.utils import is_torch_available

from .tools import Tool
from .utils import _is_package_available


if TYPE_CHECKING:
    from transformers import StoppingCriteriaList

logger = logging.getLogger(__name__)

DEFAULT_JSONAGENT_REGEX_GRAMMAR = {
    "type": "regex",
    "value": 'Thought: .+?\\nAction:\\n\\{\\n\\s{4}"action":\\s"[^"\\n]+",\\n\\s{4}"action_input":\\s"[^"\\n]+"\\n\\}\\n<end_code>',
}

DEFAULT_CODEAGENT_REGEX_GRAMMAR = {
    "type": "regex",
    "value": "Thought: .+?\\nCode:\\n```(?:py|python)?\\n(?:.|\\s)+?\\n```<end_code>",
}


def get_dict_from_nested_dataclasses(obj):
    def convert(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: convert(v) for k, v in asdict(obj).items()}
        return obj

    return convert(obj)


@dataclass
class ChatMessageToolCallDefinition:
    arguments: Any
    name: str
    description: Optional[str] = None

    @classmethod
    def from_hf_api(cls, tool_call_definition) -> "ChatMessageToolCallDefinition":
        return cls(
            arguments=tool_call_definition.arguments,
            name=tool_call_definition.name,
            description=tool_call_definition.description,
        )


@dataclass
class ChatMessageToolCall:
    function: ChatMessageToolCallDefinition
    id: str
    type: str

    @classmethod
    def from_hf_api(cls, tool_call) -> "ChatMessageToolCall":
        return cls(
            function=ChatMessageToolCallDefinition.from_hf_api(tool_call.function),
            id=tool_call.id,
            type=tool_call.type,
        )


@dataclass
class ChatMessage:
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[ChatMessageToolCall]] = None

    def model_dump_json(self):
        return json.dumps(get_dict_from_nested_dataclasses(self))

    @classmethod
    def from_hf_api(cls, message) -> "ChatMessage":
        tool_calls = None
        if getattr(message, "tool_calls", None) is not None:
            tool_calls = [ChatMessageToolCall.from_hf_api(tool_call) for tool_call in message.tool_calls]
        return cls(role=message.role, content=message.content, tool_calls=tool_calls)

    @classmethod
    def from_dict(cls, data: dict) -> "ChatMessage":
        if data.get("tool_calls"):
            tool_calls = [
                ChatMessageToolCall(
                    function=ChatMessageToolCallDefinition(**tc["function"]),
                    id=tc["id"],
                    type=tc["type"],
                )
                for tc in data["tool_calls"]
            ]
            data["tool_calls"] = tool_calls
        return cls(**data)


def parse_json_if_needed(arguments: Union[str, dict]) -> Union[str, dict]:
    if isinstance(arguments, dict):
        return arguments
    else:
        try:
            return json.loads(arguments)
        except Exception:
            return arguments


def parse_tool_args_if_needed(message: ChatMessage) -> ChatMessage:
    for tool_call in message.tool_calls:
        tool_call.function.arguments = parse_json_if_needed(tool_call.function.arguments)
    return message


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_CALL = "tool-call"
    TOOL_RESPONSE = "tool-response"

    @classmethod
    def roles(cls):
        return [r.value for r in cls]


tool_role_conversions = {
    MessageRole.TOOL_CALL: MessageRole.ASSISTANT,
    MessageRole.TOOL_RESPONSE: MessageRole.USER,
}


def get_tool_json_schema(tool: Tool) -> Dict:
    properties = deepcopy(tool.inputs)
    required = []
    for key, value in properties.items():
        if value["type"] == "any":
            value["type"] = "string"
        if not ("nullable" in value and value["nullable"]):
            required.append(key)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def remove_stop_sequences(content: str, stop_sequences: List[str]) -> str:
    for stop_seq in stop_sequences:
        if content[-len(stop_seq) :] == stop_seq:
            content = content[: -len(stop_seq)]
    return content


def get_clean_message_list(
    message_list: List[Dict[str, str]],
    role_conversions: Dict[MessageRole, MessageRole] = {},
) -> List[Dict[str, str]]:
    """
    Subsequent messages with the same role will be concatenated to a single message.

    Args:
        message_list (`List[Dict[str, str]]`): List of chat messages.
    """
    final_message_list = []
    message_list = deepcopy(message_list)  # Avoid modifying the original list
    for message in message_list:
        # if not set(message.keys()) == {"role", "content"}:
        #     raise ValueError("Message should contain only 'role' and 'content' keys!")

        role = message["role"]
        if role not in MessageRole.roles():
            raise ValueError(f"Incorrect role {role}, only {MessageRole.roles()} are supported for now.")

        if role in role_conversions:
            message["role"] = role_conversions[role]

        if len(final_message_list) > 0 and message["role"] == final_message_list[-1]["role"]:
            final_message_list[-1]["content"] += "\n=======\n" + message["content"]
        else:
            final_message_list.append(message)
    return final_message_list


class Model:
    def __init__(self, **kwargs):
        self.last_input_token_count = None
        self.last_output_token_count = None
        # Set default values for common parameters
        kwargs.setdefault("max_tokens", 4096)
        self.kwargs = kwargs

    def _prepare_completion_kwargs(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        custom_role_conversions: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> Dict:
        """
        Prepare parameters required for model invocation, handling parameter priorities.

        Parameter priority from high to low:
        1. Explicitly passed kwargs
        2. Specific parameters (stop_sequences, grammar, etc.)
        3. Default values in self.kwargs
        """
        # Clean and standardize the message list
        messages = get_clean_message_list(messages, role_conversions=custom_role_conversions or tool_role_conversions)

        # Use self.kwargs as the base configuration
        completion_kwargs = {
            **self.kwargs,
            "messages": messages,
        }

        # Handle specific parameters
        if stop_sequences is not None:
            completion_kwargs["stop"] = stop_sequences
        if grammar is not None:
            completion_kwargs["grammar"] = grammar

        # Handle tools parameter
        if tools_to_call_from:
            completion_kwargs.update(
                {
                    "tools": [get_tool_json_schema(tool) for tool in tools_to_call_from],
                    "tool_choice": "required",
                }
            )

        # Finally, use the passed-in kwargs to override all settings
        completion_kwargs.update(kwargs)

        return completion_kwargs

    def get_token_counts(self) -> Dict[str, int]:
        return {
            "input_token_count": self.last_input_token_count,
            "output_token_count": self.last_output_token_count,
        }

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        """Process the input messages and return the model's response.

        Parameters:
            messages (`List[Dict[str, str]]`):
                A list of message dictionaries to be processed. Each dictionary should have the structure `{"role": "user/system", "content": "message content"}`.
            stop_sequences (`List[str]`, *optional*):
                A list of strings that will stop the generation if encountered in the model's output.
            grammar (`str`, *optional*):
                The grammar or formatting structure to use in the model's response.
            tools_to_call_from (`List[Tool]`, *optional*):
                A list of tools that the model can use to generate responses.
            **kwargs:
                Additional keyword arguments to be passed to the underlying model.

        Returns:
            `ChatMessage`: A chat message object containing the model's response.
        """
        pass  # To be implemented in child classes!


class HfApiModel(Model):
    """A class to interact with Hugging Face's Inference API for language model interaction.

    This model allows you to communicate with Hugging Face's models using the Inference API. It can be used in both serverless mode or with a dedicated endpoint, supporting features like stop sequences and grammar customization.

    Parameters:
        model_id (`str`, *optional*, defaults to `"Qwen/Qwen2.5-Coder-32B-Instruct"`):
            The Hugging Face model ID to be used for inference. This can be a path or model identifier from the Hugging Face model hub.
        token (`str`, *optional*):
            Token used by the Hugging Face API for authentication. This token need to be authorized 'Make calls to the serverless Inference API'.
            If the model is gated (like Llama-3 models), the token also needs 'Read access to contents of all public gated repos you can access'.
            If not provided, the class will try to use environment variable 'HF_TOKEN', else use the token stored in the Hugging Face CLI configuration.
        timeout (`int`, *optional*, defaults to 120):
            Timeout for the API request, in seconds.

    Raises:
        ValueError:
            If the model name is not provided.

    Example:
    ```python
    >>> engine = HfApiModel(
    ...     model_id="Qwen/Qwen2.5-Coder-32B-Instruct",
    ...     token="your_hf_token_here",
    ...     max_tokens=5000,
    ... )
    >>> messages = [{"role": "user", "content": "Explain quantum mechanics in simple terms."}]
    >>> response = engine(messages, stop_sequences=["END"])
    >>> print(response)
    "Quantum mechanics is the branch of physics that studies..."
    ```
    """

    def __init__(
        self,
        model_id: str = "Qwen/Qwen2.5-Coder-32B-Instruct",
        token: Optional[str] = None,
        timeout: Optional[int] = 120,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.model_id = model_id
        if token is None:
            token = os.getenv("HF_TOKEN")
        self.client = InferenceClient(self.model_id, token=token, timeout=timeout)

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            grammar=grammar,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )

        response = self.client.chat_completion(**completion_kwargs)

        self.last_input_token_count = response.usage.prompt_tokens
        self.last_output_token_count = response.usage.completion_tokens
        message = ChatMessage.from_hf_api(response.choices[0].message)
        if tools_to_call_from is not None:
            return parse_tool_args_if_needed(message)
        return message


class TransformersModel(Model):
    """A class to interact with Hugging Face's Inference API for language model interaction.

    This model allows you to communicate with Hugging Face's models using the Inference API. It can be used in both serverless mode or with a dedicated endpoint, supporting features like stop sequences and grammar customization.

    > [!TIP]
    > You must have `transformers` and `torch` installed on your machine. Please run `pip install smolagents[transformers]` if it's not the case.

    Parameters:
        model_id (`str`, *optional*, defaults to `"Qwen/Qwen2.5-Coder-32B-Instruct"`):
            The Hugging Face model ID to be used for inference. This can be a path or model identifier from the Hugging Face model hub.
        device_map (`str`, *optional*):
            The device_map to initialize your model with.
        torch_dtype (`str`, *optional*):
            The torch_dtype to initialize your model with.
        trust_remote_code (bool):
            Some models on the Hub require running remote code: for this model, you would have to set this flag to True.
        kwargs (dict, *optional*):
            Any additional keyword arguments that you want to use in model.generate(), for instance `max_new_tokens` or `device`.
    Raises:
        ValueError:
            If the model name is not provided.

    Example:
    ```python
    >>> engine = TransformersModel(
    ...     model_id="Qwen/Qwen2.5-Coder-32B-Instruct",
    ...     device="cuda",
    ...     max_new_tokens=5000,
    ... )
    >>> messages = [{"role": "user", "content": "Explain quantum mechanics in simple terms."}]
    >>> response = engine(messages, stop_sequences=["END"])
    >>> print(response)
    "Quantum mechanics is the branch of physics that studies..."
    ```
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        device_map: Optional[str] = None,
        torch_dtype: Optional[str] = None,
        trust_remote_code: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        if not is_torch_available() or not _is_package_available("transformers"):
            raise ModuleNotFoundError(
                "Please install 'transformers' extra to use 'TransformersModel': `pip install 'smolagents[transformers]'`"
            )
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        default_model_id = "HuggingFaceTB/SmolLM2-1.7B-Instruct"
        if model_id is None:
            model_id = default_model_id
            logger.warning(f"`model_id`not provided, using this default tokenizer for token counts: '{model_id}'")
        self.model_id = model_id
        self.kwargs = kwargs
        if device_map is None:
            device_map = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Using device: {device_map}")
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
            self.model = AutoModelForCausalLM.from_pretrained(
                model_id,
                device_map=device_map,
                torch_dtype=torch_dtype,
                trust_remote_code=trust_remote_code,
            )
        except Exception as e:
            logger.warning(
                f"Failed to load tokenizer and model for {model_id=}: {e}. Loading default tokenizer and model instead from {default_model_id=}."
            )
            self.model_id = default_model_id
            self.tokenizer = AutoTokenizer.from_pretrained(default_model_id)
            self.model = AutoModelForCausalLM.from_pretrained(model_id, device_map=device_map, torch_dtype=torch_dtype)

    def make_stopping_criteria(self, stop_sequences: List[str]) -> "StoppingCriteriaList":
        from transformers import StoppingCriteria, StoppingCriteriaList

        class StopOnStrings(StoppingCriteria):
            def __init__(self, stop_strings: List[str], tokenizer):
                self.stop_strings = stop_strings
                self.tokenizer = tokenizer
                self.stream = ""

            def reset(self):
                self.stream = ""

            def __call__(self, input_ids, scores, **kwargs):
                generated = self.tokenizer.decode(input_ids[0][-1], skip_special_tokens=True)
                self.stream += generated
                if any([self.stream.endswith(stop_string) for stop_string in self.stop_strings]):
                    return True
                return False

        return StoppingCriteriaList([StopOnStrings(stop_sequences, self.tokenizer)])

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            grammar=grammar,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )

        messages = completion_kwargs.pop("messages")
        stop_sequences = completion_kwargs.pop("stop", None)

        max_new_tokens = (
            kwargs.get("max_new_tokens")
            or kwargs.get("max_tokens")
            or self.kwargs.get("max_new_tokens")
            or self.kwargs.get("max_tokens")
        )

        if max_new_tokens:
            completion_kwargs["max_new_tokens"] = max_new_tokens

        if stop_sequences:
            completion_kwargs["stopping_criteria"] = self.make_stopping_criteria(stop_sequences)

        if tools_to_call_from is not None:
            prompt_tensor = self.tokenizer.apply_chat_template(
                messages,
                tools=[get_tool_json_schema(tool) for tool in tools_to_call_from],
                return_tensors="pt",
                return_dict=True,
                add_generation_prompt=True,
            )
        else:
            prompt_tensor = self.tokenizer.apply_chat_template(
                messages,
                return_tensors="pt",
                return_dict=True,
            )

        prompt_tensor = prompt_tensor.to(self.model.device)
        count_prompt_tokens = prompt_tensor["input_ids"].shape[1]

        out = self.model.generate(**prompt_tensor, **completion_kwargs)
        generated_tokens = out[0, count_prompt_tokens:]
        output = self.tokenizer.decode(generated_tokens, skip_special_tokens=True)
        self.last_input_token_count = count_prompt_tokens
        self.last_output_token_count = len(generated_tokens)

        if stop_sequences is not None:
            output = remove_stop_sequences(output, stop_sequences)

        if tools_to_call_from is None:
            return ChatMessage(role="assistant", content=output)
        else:
            if "Action:" in output:
                output = output.split("Action:", 1)[1].strip()
            parsed_output = json.loads(output)
            tool_name = parsed_output.get("tool_name")
            tool_arguments = parsed_output.get("tool_arguments")
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ChatMessageToolCall(
                        id="".join(random.choices("0123456789", k=5)),
                        type="function",
                        function=ChatMessageToolCallDefinition(name=tool_name, arguments=tool_arguments),
                    )
                ],
            )


class LiteLLMModel(Model):
    """This model connects to [LiteLLM](https://www.litellm.ai/) as a gateway to hundreds of LLMs.

    Parameters:
        model_id (`str`):
            The model identifier to use on the server (e.g. "gpt-3.5-turbo").
        api_base (`str`):
            The base URL of the OpenAI-compatible API server.
        api_key (`str`):
            The API key to use for authentication.
        **kwargs:
            Additional keyword arguments to pass to the OpenAI API.
    """

    def __init__(
        self,
        model_id="anthropic/claude-3-5-sonnet-20240620",
        api_base=None,
        api_key=None,
        **kwargs,
    ):
        try:
            import litellm
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "Please install 'litellm' extra to use LiteLLMModel: `pip install 'smolagents[litellm]'`"
            )

        super().__init__(**kwargs)
        self.model_id = model_id
        # IMPORTANT - Set this to TRUE to add the function to the prompt for Non OpenAI LLMs
        litellm.add_function_to_prompt = True
        self.api_base = api_base
        self.api_key = api_key

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        import litellm

        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            grammar=grammar,
            tools_to_call_from=tools_to_call_from,
            model=self.model_id,
            api_base=self.api_base,
            api_key=self.api_key,
            **kwargs,
        )

        response = litellm.completion(**completion_kwargs)

        self.last_input_token_count = response.usage.prompt_tokens
        self.last_output_token_count = response.usage.completion_tokens

        message = ChatMessage.from_dict(
            response.choices[0].message.model_dump(include={"role", "content", "tool_calls"})
        )

        if tools_to_call_from is not None:
            return parse_tool_args_if_needed(message)
        return message


class OpenAIServerModel(Model):
    """This model connects to an OpenAI-compatible API server.

    Parameters:
        model_id (`str`):
            The model identifier to use on the server (e.g. "gpt-3.5-turbo").
        api_base (`str`, *optional*):
            The base URL of the OpenAI-compatible API server.
        api_key (`str`, *optional*):
            The API key to use for authentication.
        custom_role_conversions (`Dict{str, str]`, *optional*):
            Custom role conversion mapping to convert message roles in others.
            Useful for specific models that do not support specific message roles like "system".
        **kwargs:
            Additional keyword arguments to pass to the OpenAI API.
    """

    def __init__(
        self,
        model_id: str,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        custom_role_conversions: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        try:
            import openai
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "Please install 'openai' extra to use OpenAIServerModel: `pip install 'smolagents[openai]'`"
            ) from None

        super().__init__(**kwargs)
        self.model_id = model_id
        self.client = openai.OpenAI(
            base_url=api_base,
            api_key=api_key,
        )
        self.custom_role_conversions = custom_role_conversions

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            grammar=grammar,
            tools_to_call_from=tools_to_call_from,
            model=self.model_id,
            custom_role_conversions=self.custom_role_conversions,
            **kwargs,
        )

        response = self.client.chat.completions.create(**completion_kwargs)
        self.last_input_token_count = response.usage.prompt_tokens
        self.last_output_token_count = response.usage.completion_tokens

        message = ChatMessage.from_dict(
            response.choices[0].message.model_dump(include={"role", "content", "tool_calls"})
        )
        if tools_to_call_from is not None:
            return parse_tool_args_if_needed(message)
        return message


class AzureOpenAIServerModel(OpenAIServerModel):
    """This model connects to an Azure OpenAI deployment.

    Parameters:
        model_id (`str`):
            The model deployment name to use when connecting (e.g. "gpt-4o-mini").
        azure_endpoint (`str`, *optional*):
            The Azure endpoint, including the resource, e.g. `https://example-resource.azure.openai.com/`. If not provided, it will be inferred from the `AZURE_OPENAI_ENDPOINT` environment variable.
        api_key (`str`, *optional*):
            The API key to use for authentication. If not provided, it will be inferred from the `AZURE_OPENAI_API_KEY` environment variable.
        api_version (`str`, *optional*):
            The API version to use. If not provided, it will be inferred from the `OPENAI_API_VERSION` environment variable.
        custom_role_conversions (`Dict[str, str]`, *optional*):
            Custom role conversion mapping to convert message roles in others.
            Useful for specific models that do not support specific message roles like "system".
        **kwargs:
            Additional keyword arguments to pass to the Azure OpenAI API.
    """

    def __init__(
        self,
        model_id: str,
        azure_endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
        custom_role_conversions: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        # read the api key manually, to avoid super().__init__() trying to use the wrong api_key (OPENAI_API_KEY)
        if api_key is None:
            api_key = os.environ.get("AZURE_OPENAI_API_KEY")

        super().__init__(
            model_id=model_id,
            api_key=api_key,
            custom_role_conversions=custom_role_conversions,
            **kwargs,
        )
        # if we've reached this point, it means the openai package is available (checked in baseclass) so go ahead and import it
        import openai

        self.client = openai.AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=azure_endpoint)


class DeepSeekReasonerModel(OpenAIServerModel):
    """This model connects to the DeepSeek API server, emulating tool calls via system prompt instructions and response parsing.

    > [!NOTE]
    > DeepSeek API does not natively support tool calls. This model emulates tool calling by:
    > 1. Injecting a system message describing available tools and instructing the model to use them.
    > 2. Parsing the model's response content to extract tool calls based on a predefined format (similar to TransformersModel).

    Parameters:
        model_id (`str`):
            The model id for deepseek models (e.g., "deepseek-reasoner").
        api_base (`str`, *optional*):
            The base URL of the DeepSeek API server.
        api_key (`str`, *optional*):
            The API key to use for authentication.
        **kwargs:
            Additional keyword arguments to pass to the OpenAI API.
    """

    def __init__(
        self,
        model_id: str = "deepseek-reasoner",
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(model_id=model_id, api_base=api_base, api_key=api_key, **kwargs)

    def __call__(
        self,
        messages: List[Dict[str, str]],
        stop_sequences: Optional[List[str]] = None,
        grammar: Optional[str] = None,
        tools_to_call_from: Optional[List[Tool]] = None,
        **kwargs,
    ) -> ChatMessage:
        augmented_messages = deepcopy(messages)
        if tools_to_call_from:
            tool_description = "You have access to the following tools:\n"
            for tool in tools_to_call_from:
                tool_schema = get_tool_json_schema(tool)["function"]
                tool_description += f"- Tool Name: {tool_schema['name']}\n"
                tool_description += f"  Description: {tool_schema['description']}\n"
                tool_description += f"  Parameters: {tool_schema['parameters']}\n"
                tool_description += "\n"
            tool_description += (
                "When you need to use a tool, please respond with a JSON object enclosed in text after 'Action:' keyword in the following format:\n"
                'Thought: ...\nAction:\n{\n  "action": "tool_name",\n  "action_input": {"arg1": "value1", "arg2": "value2"}\n}\n<end_code>\n'
                "If you don't need to use any tools, just respond as a normal assistant."
            )
            system_message = {"role": "system", "content": tool_description}
            augmented_messages.insert(0, system_message)

        completion_kwargs = self._prepare_completion_kwargs(
            messages=augmented_messages,
            stop_sequences=stop_sequences,
            grammar=grammar,
            tools_to_call_from=None,
            model=self.model_id,
            **kwargs,
        )

        response = self.client.chat.completions.create(**completion_kwargs)
        self.last_input_token_count = response.usage.prompt_tokens
        self.last_output_token_count = response.usage.completion_tokens

        response_message = response.choices[0].message
        message = ChatMessage.from_dict(response_message.model_dump(include={"role", "content"}))

        if tools_to_call_from is not None:
            try:
                tool_calls = []
                content = response_message.content or ""
                action_blocks = content.split("Action:")  # split into blocks by "Action:" keyword

                for block in action_blocks[1:]:  # process each block after the first one
                    if not block.strip():  # skip empty blocks
                        continue
                    json_str = None
                    if "<end_code>" in block:
                        json_str = block.split("<end_code>")[0].strip()  # extract json string before <end_code>
                    else:
                        logger.warning(
                            "`<end_code>` not found in DeepSeek Action block, attempting to parse the whole block as JSON."
                        )
                        json_str = block.strip()  # if no <end_code>, try to parse the whole block as json

                    if json_str:
                        try:
                            # Remove <end_code> if present before parsing JSON (already done above, but for safety)
                            if json_str.endswith("<end_code>"):
                                json_str = json_str[: -len("<end_code>")].strip()
                            json_str = json_str.strip()  # strip again to handle potential whitespace around json
                            # Attempt to parse with relaxed JSON parsing (using json.loads with strict=False)
                            try:
                                parsed_output = json.loads(json_str)
                            except json.JSONDecodeError as e:
                                logger.warning(
                                    f"Standard json.loads failed, trying json.loads with `strict=False`: {e}"
                                )
                                parsed_output = json.loads(
                                    json_str, strict=False
                                )  # try with strict=False to allow control characters etc.

                            tool_name = parsed_output.get("action") or parsed_output.get(
                                "tool_name"
                            )  # try action first, then tool_name
                            tool_arguments = parsed_output.get("action_input") or parsed_output.get(
                                "tool_arguments"
                            )  # try action_input first, then tool_arguments
                            if tool_name and tool_arguments is not None:
                                tool_calls.append(
                                    ChatMessageToolCall(
                                        id="".join(random.choices("0123456789", k=5)),
                                        type="function",
                                        function=ChatMessageToolCallDefinition(
                                            name=tool_name, arguments=tool_arguments
                                        ),
                                    )
                                )
                            else:
                                logger.warning(
                                    f"Could not extract tool name and arguments from DeepSeek response JSON: {parsed_output}, json_str: {json_str}"
                                )  # include json_str in log

                        except json.JSONDecodeError as e:
                            logger.warning(
                                f"Failed to parse tool call JSON from DeepSeek response block, even with `strict=False`: {json_str}, error: {e}"
                            )  # log raw output for inspection
                if not tool_calls:
                    message.content = content  # if no tool calls extracted, set content to full response content
                else:
                    message.content = (
                        content.split("Action:")[0].strip() if "Action:" in content else content.strip()
                    )  # keep the content before "Action:" as message content
                    message.tool_calls = tool_calls
                return message
            except Exception as e:
                logger.error(f"Failed to parse DeepSeek response for tool calls: {e}")
                return response_message
        else:
            return response_message


__all__ = [
    "MessageRole",
    "tool_role_conversions",
    "get_clean_message_list",
    "Model",
    "TransformersModel",
    "HfApiModel",
    "LiteLLMModel",
    "OpenAIServerModel",
    "AzureOpenAIServerModel",
    "DeepSeekReasonerModel",
    "ChatMessage",
]
