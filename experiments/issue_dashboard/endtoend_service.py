"""Opt-in ranked service clocks and pool/durability diagnostics for the bridge."""
import time
from .distribution import DistributionRankingService
from .ranking_design import RankingDesignService
from .service import IssueService


class ResourceMixin:
    def page(self, project_id=0, state='open', limit=20, offset=0):
        with self.pool.acquire() as (connection, wait_ms):
            started = time.perf_counter()
            result = self.fixture.ranked_on(connection, self.config['query_kind'], project_id, state, limit, offset)
            result['backend'] = {'pool_wait_ms': wait_ms,
                'transaction_wall_ms': (time.perf_counter() - started) * 1000, 'backend_pid': connection.info.backend_pid}
            return result

    def diagnostics(self):
        value = IssueService.diagnostics(self)
        # The inherited diagnostics also execute legacy recent-page EXPLAINs.
        # Do not label those plans as the measured ranked query.
        value.pop('page_plan'); value.pop('count_plan')
        return {**value, 'read_operation': 'discussion_rank', 'resource_policy': 'ranked-resource-v1'}


class DistributionResourceService(ResourceMixin, DistributionRankingService): pass
class DesignResourceService(ResourceMixin, RankingDesignService): pass
