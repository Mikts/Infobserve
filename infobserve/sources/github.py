import asyncio
from typing import Any, Dict, List, Optional, Union

import aiohttp

from infobserve.common import APP_LOGGER
from infobserve.common.index_cache import IndexCache
from infobserve.common.queue import ProcessingQueue
from infobserve.events import GithubEvent

from .base import SourceBase

BAD_CREDENTIALS = "Bad credentials"


class GithubSource(SourceBase):
    """The implementation of Github Source.

    This Class represents the github as a source of data it fetches
    the latest number of events specified in config and creates list of those
    events represented as GithubEvent objects.

    Attributes:
        SOURCE_TYPE (string): The type of the source.
        _oauth_token (string): The oauth token for the github api.
        _username (string): The username of the user to authenticate.
        _uri (string): Gitlab's api uri.
        _api_version (string): Gitlab's api version.
        _index_cache(infobserve.common.index_cache.IndexCache): IndexCache object to query the postgres cache
        _timeout(float): The frequency the gists endpoint is queried
        _etag(str): Returns no data if no changes detected in the api.
    """

    def __init__(self, config: Dict, name: str = None):
        SourceBase.__init__(self, name=name)
        self.SOURCE_TYPE: str = "github-public-events"
        self._oauth_token: Optional[Any] = config.get('oauth')
        self._username: Optional[Any] = config.get('username')
        self._uri: str = "https://api.github.com/events"
        self._api_version: str = "application/vnd.github.v3+json"
        self._index_cache: IndexCache = IndexCache(self.SOURCE_TYPE)
        self.timeout: Union[float] = config.get('timeout', 60)
        self._etag: Optional[Any] = None

    async def fetch_events(self) -> List[GithubEvent]:
        """
        Fetches the most recent gists created.

        Returns:
            event_list (list) : A list of GistEvent Objects.
        """

        headers: Dict = {
            "User-Agent": 'Infobserver',
            "Accept": self._api_version,
            "Authorization": f'token {self._oauth_token}'
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            resp = await session.get(self._uri, headers=headers)
            APP_LOGGER.debug("GithubSource Response Headers: %s", resp.headers)
            github_events = await resp.json()
            event_list = []
            tasks = []

            APP_LOGGER.debug("GithubSource: %s Fetched Recent %s Public Events", self.name, len(github_events))

            if self._index_cache:
                cached_ids = await self._index_cache.query_index_cache()
                # At the moment only PushEvent support we should productize more logic into helper classes
                # To support all the event types.
                github_events = [x for x in github_events if x["type"] == "PushEvent"]
                APP_LOGGER.debug("GithubSource: %s Found %s of type PushEvent", self.name, len(github_events))
                github_events = [x for x in github_events if x["id"] not in cached_ids]

            APP_LOGGER.debug("Github Events number not in cache: %s", len(github_events))

            for event in github_events:
                # Create GistEvent objects and create io intensive tasks.
                ge = GithubEvent(event, session)

                try:
                    event_list.append(ge)
                    tasks.append(asyncio.create_task(ge.get_raw_content()))
                except asyncio.TimeoutError:
                    APP_LOGGER.warning("Dropped event with id:%s url not valid", ge.id)
            # Check the index_cache.
            if self._index_cache:
                await self._index_cache.update_index_cache([x["id"] for x in github_events])

            # Fetch the commits async.
            await asyncio.gather(*tasks)

            APP_LOGGER.debug("%s GithubEvents send for processing", len(github_events))
            return event_list

    async def fetch_events_scheduled(self, queue: ProcessingQueue):
        """
        Call the fetch_events method on a schedule.

        Arguments:
           queue (ProcessingQueue): A processing queue to enqueue the events.
        """
        while True:
            try:
                events: List[GithubEvent] = await self.fetch_events()
                for event in events:
                    await queue.queue_event(event)
            except aiohttp.client_exceptions.ClientPayloadError:
                APP_LOGGER.warning("There was an error retrieving the payload will retry in next cycle.")

            await asyncio.sleep(self.timeout)
