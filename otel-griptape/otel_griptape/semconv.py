"""gen_ai.* attribute name constants and small lookup tables.

These names are pinned to what Section 4.3.1 of the build spec (R1, R6)
expects the Guardian's rule engine to query for. Do not rename these
without updating the rule engine to match.
"""

from __future__ import annotations

# --- Required GenAI semantic-convention attributes (R1) ---------------------
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

# Fields R1 requires be present on every gen_ai-kind span. Keep this in sync
# with the constants above -- it's what instrumentor.py checks before
# deciding whether to mark a span as "gen_ai-kind" at all.
REQUIRED_GEN_AI_FIELDS = (
    GEN_AI_SYSTEM,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_RESPONSE_FINISH_REASONS,
)

# --- Tool-call attributes -----------------------------------------------
GEN_AI_TOOL_NAME = "gen_ai.tool.name"

# --- R6 payload-tracking attributes (not an OTel standard -- a convention
# invented for this project; see Section 9 of the build spec) ---------------
PAYLOAD_RAW_BYTES = "payload.raw_bytes"
PAYLOAD_CAPTURED_BYTES = "payload.captured_bytes"

# --- gen_ai.operation.name values used by this library ----------------------
OPERATION_CHAT = "chat"
OPERATION_EXECUTE_TOOL = "execute_tool"
OPERATION_INVOKE_AGENT = "invoke_agent"

# Maps a griptape prompt-driver class name to the well-known gen_ai.system
# value for that provider. Anything not in this table falls back to a
# lowercased, "_prompt_driver"-stripped version of the class name so new
# griptape drivers still get *a* reasonable value rather than nothing.
_DRIVER_CLASS_TO_GEN_AI_SYSTEM: dict[str, str] = {
    "OpenAiChatPromptDriver": "openai",
    "AzureOpenAiChatPromptDriver": "az.ai.openai",
    "AnthropicPromptDriver": "anthropic",
    "AmazonBedrockPromptDriver": "aws.bedrock",
    "AmazonSageMakerJumpstartPromptDriver": "aws.sagemaker",
    "GooglePromptDriver": "gcp.gemini",
    "CoherePromptDriver": "cohere",
    "OllamaPromptDriver": "ollama",
    "GriptapeCloudPromptDriver": "griptape_cloud",
    "HuggingFaceHubPromptDriver": "huggingface",
    "HuggingFacePipelinePromptDriver": "huggingface",
    "DummyPromptDriver": "dummy",
}


def gen_ai_system_for_driver(driver: object) -> str:
    """Best-effort mapping from a griptape prompt-driver instance to a
    gen_ai.system value. Known drivers use the OTel-recognized well-known
    value; unknown ones fall back to a normalized class name rather than
    silently omitting gen_ai.system (an omission would itself be an R1
    violation, which would be a strange thing for this library to cause).
    """
    class_name = type(driver).__name__
    if class_name in _DRIVER_CLASS_TO_GEN_AI_SYSTEM:
        return _DRIVER_CLASS_TO_GEN_AI_SYSTEM[class_name]

    normalized = class_name
    for suffix in ("PromptDriver",):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    # CamelCase -> snake_case, roughly
    out = []
    for i, ch in enumerate(normalized):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out) or "unknown"
