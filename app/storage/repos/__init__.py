"""Repositories: the only code that writes SQL.

Each repository owns one aggregate and takes a :class:`Database`. Route handlers
call these from synchronous (``def``) handlers so FastAPI offloads the blocking
SQLite work to a threadpool.
"""

from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.guard import GuardLogRepo
from app.storage.repos.jobs import JobRepo
from app.storage.repos.prompts import PromptRepo

__all__ = ["BuildRepo", "EndpointRepo", "GuardLogRepo", "JobRepo", "PromptRepo"]
