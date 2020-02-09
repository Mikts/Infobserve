import asyncio
import sys
from enum import Enum
from pathlib import Path

import yara

from infobserve.common import APP_LOGGER
from infobserve.common.queue import ProcessingQueue

# TODO: Add exception handlers. Async functions don't notify anyone
#       when they fail, so the whole script hangs


class YaraProcessor:
    """
    Consumes Sources from the Processing queue, passes them through the
    Yara matching engine and adds them to the DB Queue (TBI)
    """

    def __init__(self, rule_files, source_queue, db_queue):
        """
        Args:
            rule_files (list): A list of paths to the Yara rule files
                               Each item can contain wildcards.
            source_queue (infobserve.processing.queue.ProcessingQueue):
                        An instance of the queue in which Source objects will
                        be retrieved from
            db_queue (infobserve.processing.queue.ProcessingQueue):
                        An instance of the queue in which Event objects will
                        be inserted into
        """
        self._processing = False
        self._rules = {}

        self._source_queue = source_queue
        self._db_queue = db_queue
        self._cmd_queue = ProcessingQueue()

        # Generate rules along with their namespaces
        self._rules = self._generate_rules(rule_files)
        self._engine = self._compile_rules()

    async def process(self):
        """
        The consumer function for the source queue.
        Removes Sources from the Processing Queue, tries to match them
        against the provided Yara rules. Places any matches into the Database
        Queue for further processing and storage (TBI - Right now, it simply
        logs matches)
        """
        APP_LOGGER.info("Processing started using the Yara Engine")
        self._processing = True

        # A (practically) infinite amount of items will be processed
        # unless a STOP command is received, in which case, any remaining items will be
        # processed and then processing will stop
        items_remaining = sys.maxsize
        items_processed = 0
        while items_processed < items_remaining:
            remaining = [self._source_queue.get_event(), self._cmd_queue.get_event()]
            APP_LOGGER.debug("Polling cmd and source queues. Remaining: %s", remaining)
            completed_tasks, _ = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            for completed_task in completed_tasks:
                event = await completed_task

                if isinstance(event, YaraProcessor._Command):
                    if event == YaraProcessor._Command.RECOMPILE:
                        APP_LOGGER.info("Recompile command received")
                        self._processing = False
                        self.compile_rules()
                        self._processing = True
                    elif event == YaraProcessor._Command.STOP:
                        # When a STOP command is received, all remaining items in the queue will be processed
                        # and then processing will stop
                        items_remaining = self._source_queue.events_left()
                        APP_LOGGER.info(
                            "Stop command received. Will stop %s",
                            "immediately" if items_remaining == 0 else f"after processing {items_remaining} items")
                    self._cmd_queue.notify()
                else:
                    APP_LOGGER.debug("Processing new event")
                    items_processed += 1

                    matches = self._engine.match(data=event.raw_content)

                    for match in matches:
                        APP_LOGGER.debug(
                            """
                            ======= Match ======
                            Rule matched: %s
                            Tags: %s
                            Strings: %s
                            """, match.rule, match.tags, match.strings)

                    self._source_queue.notify()

    async def add_rules(self, rule_files, append=True, recompile=False):
        """
        Parses additional rules and stores them as a class attribute.
        Note that this function does *not* necessarily re-compile the Yara processor

        Args:
            rule_files (list[str]): A list of paths to the rule files that will be loaded
            append (bool): If True, the new rules will be appended to the old ones, otherwise, the old ones
                           will be replaced
            recompile (bool): If True, the Yara processor will be re-compiled after loading the new rules
                              It can be re-compiled separately using `YaraProcessor.compile_rules`
        """

        APP_LOGGER.info("Refreshing Yara rules (%s)", "Appending" if append else "Replacing")

        new_rules = {filepath: filepath for filepath in self._get_file_sources(rule_files)}

        if append:
            self._rules.update(new_rules)
        else:
            self._rules = new_rules

        if recompile:
            await self.compile_rules()

    async def compile_rules(self, immediately=False, block=False):
        """
        (Re)Compiles the Yara rules using the rule files provided in the constructor or in `add_rules`.
        If the processing method is running, Depending on the `force` argument,
        it will either simply compile the Yara rules or tell the async method to recompile.

        Args:
            immediately (bool): If True, the processor will simply be recompiled. This could be very dangerous if
                                the `process` method is already running because thread safety is not guaranteed.
                                If False, a Recompile command will be added to the processing queue and when the
                                `process` method pops it from the queue, then the processor will be re-compiled safely.
            block (bool): If True and `force` is False, then the process will block until the consumer method
                          has popped the event out of the queue and notified that it was done recompiling
                          (needs further testing)
        """

        APP_LOGGER.info("Recompiling Yara rules")
        if not self._processing or immediately:
            self._engine = self._compile_rules()
        else:
            await self._cmd_queue.queue_event(YaraProcessor._Command.RECOMPILE)
            if block:
                await self._cmd_queue.wait_all()

    async def stop_processing(self, immediately=False):
        """
        Stops all processing either immediately, or after current items have been processed.

        Args:
            immediately (bool): If True, then all items from the processing queue will be popped without them being
                                processed. Otherwise, processing will stop only after it has processed all items in the
                                queue at the time that this function was called. Default: False
        """

        APP_LOGGER.info("STOP signal received. Notifying processing method")
        if not immediately:
            await self._cmd_queue.queue_event(YaraProcessor._Command.STOP)
        else:
            for _ in range(self._source_queue.events_left()):
                # Drop all current items
                await self._source_queue.get()
                self._source_queue.notify()

            await self._cmd_queue.queue_event(YaraProcessor._Command.STOP)

    def _compile_rules(self):
        """
        Compiles the loaded Yara rules
        """
        APP_LOGGER.info("Recompiling Yara rules")
        return yara.compile(filepaths=self._rules)

    def _generate_rules(self, rule_files):
        """
        Returns a dictionary containing the Namespace to rulefile mapping

        Args:
            rule_files (list[str]): The list of rule file paths to resolve
        """
        return {filename: filename for filename in self._get_file_sources(rule_files)}

    def _get_file_sources(self, rule_files):
        """
        Resolves the paths provided in the `rule_files` list. Also expands
        any `*` found in the paths using pathlib.Path

        Args:
            rule_files (list[str]): The list of rule file paths to resolve
        Returns:
            A generator object that yields each resolved path as a string
        """
        for rule_file in rule_files:
            filepath = Path(rule_file)
            if filepath.is_file():
                yield filepath.as_posix()
            else:
                for inner_file in Path().glob(rule_file):
                    yield inner_file.as_posix()

    class _Command(Enum):
        RECOMPILE = 1
        STOP = 2
