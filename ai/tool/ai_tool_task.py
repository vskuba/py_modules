from queue_.queue_ import queue_get


def task_abort(final_text: str) -> bool:
    """
    This function close active task. Invoke this tool only if you have instruction exactly 'abort task'.
    In case if you have instruction 'finish task', 'complete task' etc do not use this tool.
    Argument final_text is used for display in task report.
    """
    queue_get('task_abort').put(True)
    queue_get('chat').put(
        {
            "text": final_text,
            "who": 'agent',
        }
    )

    return True