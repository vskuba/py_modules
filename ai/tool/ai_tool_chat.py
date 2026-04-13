import asyncio

from async_.async_ import async_waiting_start
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


async def chat_me_question(question: str) -> str:
    """
    This tool allows you to ask the user a question and receive an answer.
    The function argument is 'question'. The function will return a text string containing the answer to the question.
    """
    queue_get()['chat'].put(
        {
            "text": question,
            "who": 'agent',
            "need_response": True
        }
    )

    logger_info('Задача уходит в режим ожидания моего ответа на вопрос: "%s"' % question)

    await async_waiting_start()

    await asyncio.sleep(0.1)
    chat_last_message = queue_get()['chat_response'].get_nowait()

    return f"Пользователь ответил '{chat_last_message}'"


async def chat_me(text: str) -> str:
    """
    This is a messaging tool. Use it to send a message.
    The argument is a text string containing your message.
    The tool will return the word 'Accepted' as a result, indicating that the message was delivered to the user.
    """
    queue_get()['chat'].put(
        {
            "text": text,
            "who": 'agent',
        }
    )

    return 'Accepted'
