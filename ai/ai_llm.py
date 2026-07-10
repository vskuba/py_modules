LLM_PROVIDERS = ('openrouter', 'claude', 'ollama', 'gemini', 'groq', 'mistral', 'huggingface', 'cerebras')


def ai_llm_provider(model_name: str) -> str | None:
    for provider in LLM_PROVIDERS:
        if (model_name or '').lower().startswith(provider + '/'):
            return provider
    return None
