-- Add memory_layer column for memory hierarchy (fact, observation, model).
ALTER TABLE astrocytes_vectors ADD COLUMN IF NOT EXISTS memory_layer TEXT;
