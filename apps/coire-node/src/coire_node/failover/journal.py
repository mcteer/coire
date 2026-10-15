"""Native compatibility import for the shared durable election journal."""

from coire_core.failover_journal import ElectionJournal, ElectionJournalError

__all__ = ["ElectionJournal", "ElectionJournalError"]
