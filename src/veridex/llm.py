from functools import cache

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from veridex.config import settings


@cache
def get_llm() -> BaseChatModel:
    """
    Return the shared chat model used by every node and skill.

    Any OpenAI-compatible chat completions API works; provider, model and key are settings. The model is
    created lazily so the app can be imported (e.g. in tests) without an API key configured.

    :return: A deterministic (temperature 0) chat model.
    """
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


def get_structured_llm[T: BaseModel](schema: type[T]) -> Runnable:
    """
    Return the shared chat model bound to a structured output schema.

    Uses tool calling rather than ``json_schema`` response formats: on Scaleway's models the latter is enforced
    but the field descriptions do not reach the model, which then misreads fields such as the page type.

    :param schema: Pydantic model the LLM response is parsed into.
    :return: A runnable whose ``ainvoke`` returns an instance of ``schema``.
    """
    return get_llm().with_structured_output(schema, method="function_calling")
