"""Однособытийный адаптер тестов для процессной сборки каналов."""

from compadre_assistant.bootstrap import create_channel_message_processor


def process_channel_incoming_message(*, incoming_message, completion_handler=None, **options):
    """Выполняет событие через новую сборку без тестового polling цикла."""
    with create_channel_message_processor(**options) as processor:
        return processor(incoming_message, completion_handler)
