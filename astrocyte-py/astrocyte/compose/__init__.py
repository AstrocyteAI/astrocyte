"""Composition layer — cross-engine associations.

This package contains types and SPIs that span more than one engine.
Neither the Document Engine nor the Conversation Engine imports from
here; the composition layer imports from both.

Currently:
  ConversationDocumentLink        — data type for a conversation ↔ document association
  ConversationDocumentStore       — SPI for attach / detach / lookup
  InMemoryConversationDocumentStore — in-memory impl for tests
"""

from astrocyte.compose.conversation_document import (
    ConversationDocumentLink,
    ConversationDocumentStore,
    InMemoryConversationDocumentStore,
)

__all__ = [
    "ConversationDocumentLink",
    "ConversationDocumentStore",
    "InMemoryConversationDocumentStore",
]
