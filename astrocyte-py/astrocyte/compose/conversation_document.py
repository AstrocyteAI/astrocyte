"""ConversationDocumentStore — many-to-many link between conversations and documents.

Composition-layer SPI. Lives here (not in either engine) because it
spans both the Conversation Engine and the Document Engine without
belonging to either.

This is what resolves the Path 3 (DE + CE → ME) composition scenario:
  - At ingest time: call attach(conversation_id, document_id) after
    uploading a document to a session.
  - At recall time: call documents_for_conversation(conversation_id)
    to get the doc_ids to pass to DocumentNavigator.search().

The Postgres implementation lives in adapters-storage-py/astrocyte-postgres/
and backs this SPI with migration 031 (astrocyte_conversation_documents).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ConversationDocumentLink:
    """One row in the conversation ↔ document association table."""

    conversation_id: str
    document_id: str
    attached_at: datetime
    attached_by: str | None = None  # optional actor/speaker id


class ConversationDocumentStore(ABC):
    """SPI for the conversation ↔ document many-to-many relationship.

    Cascade semantics (enforced by the Postgres implementation via FK):
      detach(conversation_id, document_id)
        → removes the structural link; document remains in the bank
          and is still searchable via memory.recall(). Only an explicit
          DocumentStore.delete_document() removes it from the bank.

      delete conversation → CASCADE removes all its document associations
          (documents themselves are unaffected)

      delete document → CASCADE removes associations from all conversations
          (conversations themselves are unaffected)
    """

    @abstractmethod
    async def attach(
        self,
        conversation_id: str,
        document_id: str,
        *,
        attached_by: str | None = None,
    ) -> ConversationDocumentLink:
        """Associate a document with a conversation.

        Idempotent — attaching an already-attached document updates
        attached_at and returns the link.
        """

    @abstractmethod
    async def detach(self, conversation_id: str, document_id: str) -> None:
        """Remove the structural link between a conversation and a document.

        No-op if the link doesn't exist. Does NOT delete the document.
        """

    @abstractmethod
    async def documents_for_conversation(self, conversation_id: str) -> list[str]:
        """All document_ids attached to this conversation, ordered by attached_at asc."""

    @abstractmethod
    async def conversations_for_document(self, document_id: str) -> list[str]:
        """All conversation_ids that reference this document, newest first."""


# ── In-memory implementation (tests, embedded use) ────────────────────────────


class InMemoryConversationDocumentStore(ConversationDocumentStore):
    """Pure-Python implementation backed by dicts. Not thread-safe."""

    def __init__(self) -> None:
        # (conversation_id, document_id) → ConversationDocumentLink
        self._links: dict[tuple[str, str], ConversationDocumentLink] = {}

    async def attach(
        self,
        conversation_id: str,
        document_id: str,
        *,
        attached_by: str | None = None,
    ) -> ConversationDocumentLink:
        key = (conversation_id, document_id)
        link = ConversationDocumentLink(
            conversation_id=conversation_id,
            document_id=document_id,
            attached_at=datetime.now(timezone.utc),
            attached_by=attached_by,
        )
        self._links[key] = link
        return link

    async def detach(self, conversation_id: str, document_id: str) -> None:
        self._links.pop((conversation_id, document_id), None)

    async def documents_for_conversation(self, conversation_id: str) -> list[str]:
        links = [
            link for (cid, _), link in self._links.items()
            if cid == conversation_id
        ]
        links.sort(key=lambda link: link.attached_at)
        return [link.document_id for link in links]

    async def conversations_for_document(self, document_id: str) -> list[str]:
        links = [
            link for (_, did), link in self._links.items()
            if did == document_id
        ]
        links.sort(key=lambda link: link.attached_at, reverse=True)
        return [link.conversation_id for link in links]
