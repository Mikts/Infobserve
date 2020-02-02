"""The main entrypoint and interface of the infobserver application.
"""
import asyncio

from .common import CONFIG
from .common import APP_LOGGER
from .common.queue import ProcessingQueue
from .sources import SOURCE_FACTORY
from .processors.yara_processor import YaraProcessor

__version__ = '0.1.0'


def init_sources(config):
    sources = list()
    for conf_source in config:
        APP_LOGGER.debug("conf_source:%s", conf_source)
        sources.append(SOURCE_FACTORY.get_source(conf_source))
    return sources


def source_scheduler(sources, loop, source_queue, pool=None):
    for source in sources:
        APP_LOGGER.debug("Scheduling Source:%s", source.name)
        loop.create_task(source.fetch_events_scheduled(source_queue, pool))

    return loop


def consumer_scheduler(loop, source_queue, db_queue):
    """
    Creates a YaraProcessor, passing it the Yara rule file paths as read from the config file.

    Args:
        loop (asyncio loop): The loop to add the consumer as a task to
        source_queue (infobserve.common.queue.ProcessingQueue): The queue from which the processor will retrieve
                                                                sources
        db_queue (infobserve.common.queue.ProcessingQueue): The queue into which the processor will place any
                                                            matches
    """
    APP_LOGGER.debug("Starting Yara Processor")
    consumer = YaraProcessor(CONFIG.YARA_RULES_PATHS, source_queue, db_queue)
    loop.create_task(consumer.process())

    return loop


def main():
    APP_LOGGER.info("Logging up and running")
    APP_LOGGER.debug("Configured Sources:%s", CONFIG.SOURCES)
    source_queue = ProcessingQueue(CONFIG.PROCESSING_QUEUE_SIZE)
    # TODO: Add DB queue size option in the config?
    db_queue = ProcessingQueue()

    # Initialize Yara Processing queue

    main_loop = asyncio.get_event_loop()

    pg_pool = main_loop.run_until_complete(CONFIG.init_db_pool())

    main_loop.run_until_complete(CONFIG.init_db(pg_pool))
    APP_LOGGER.info("Initialized Schema")

    main_loop = source_scheduler(init_sources(CONFIG.SOURCES), main_loop, source_queue, pg_pool)
    main_loop = consumer_scheduler(main_loop, source_queue, db_queue)

    APP_LOGGER.debug("Consumer Scheduled")
    APP_LOGGER.info("Main Loop Initialized")
    main_loop.run_forever()


if __name__ == "__main__":
    main()
