from discord import ui
import discord
from datetime import datetime
from typing import List, Optional
import logging
import weakref



logger = logging.getLogger(__name__)


class SafeView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cog_ref     = None
        self._interaction = None

    def _set_refs(self, cog=None, original_interaction=None):
        try:
            self._cog_ref = weakref.ref(cog) if cog is not None else None
        except TypeError:
            self._cog_ref = lambda: cog
        self._interaction = original_interaction

    def get_cog(self):
        return self._cog_ref() if self._cog_ref else None

    async def on_timeout(self):
        try:
            self.stop()
        except (RuntimeError, discord.InvalidArgument):
            pass
        self._cog_ref     = None
        self._interaction = None


class SafeModal(ui.Modal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cog_ref = None

    def _set_refs(self, cog=None, **_):
        try:
            self._cog_ref = weakref.ref(cog) if cog is not None else None
        except TypeError:
            self._cog_ref = lambda: cog

    def get_cog(self):
        return self._cog_ref() if self._cog_ref else None


class ReportState:
    def __init__(self, operation_type: str, reporter_id: str = None):
        self.operation_type       = operation_type
        self.reporter_id          = reporter_id
        self.step                 = 1
        self.all_members: list    = []
        self.message              = None
        self.leaders:        List[int] = []
        self.airforce:       List[int] = []
        self.participants:   List[int] = []
        self.date_time:      str  = datetime.now().strftime("%d/%m/%Y %H:%M")
        self.duration:       Optional[str] = None
        self.result:         Optional[str] = None
        self.at_level:       Optional[str] = None
        self.activity:       Optional[str] = None
        self.arrests:        Optional[str] = None
        self.enemies:        List[str] = []
        self.af_pilot:       Optional[int] = None
        self.spawn_type:     Optional[str] = None
        self.against_name:   Optional[str] = None
        self.attempts:       Optional[str] = None
        self.reason:         Optional[str] = None
        self.notes:          Optional[str] = None
        self.screenshot:     Optional[str] = None
        self.edit_mode:      bool = False
        self.original_report_id:  Optional[int] = None
        self.operation_report_id: Optional[int] = None
        self.old_leaders:         List[int] = []
        self.old_participants:     List[int] = []
        self.old_airforce:         List[int] = []
        self.old_af_pilot:         Optional[int] = None
        self._member_dict:         Optional[dict] = None


def _has_manage_reports_permission(interaction: discord.Interaction) -> bool:
    admin_cog = interaction.client.get_cog("Admin")
    if not admin_cog:
        return False
    if admin_cog._has_acl_permission(interaction.user.id, "manage_reports"):
        return True
    for role in (interaction.user.roles or []):
        if admin_cog._has_role_acl_permission(role.id, "manage_reports"):
            return True
    return False
