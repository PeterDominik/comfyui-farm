"""ShotGrid lookups for the publish fields (read only).

The manager is the only part of the farm that talks to ShotGrid, with its own read-only
API script. The dashboard and the Nuke panel get the lists from the manager's API
(/api/v1/shotgrid/...), so neither needs shotgun_api3 or the key.

  lookup.py  shows (active projects), a show's sequences and shots, cached; checking that
             a publish goes to a shot that exists"""

from .lookup import ShotGrid

__all__ = ["ShotGrid"]
