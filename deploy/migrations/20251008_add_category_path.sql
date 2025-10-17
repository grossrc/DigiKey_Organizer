-- Migration: add hierarchical category path columns
-- Run manually: psql -d yourdb -f this_file.sql
ALTER TABLE public.parts
    ADD COLUMN IF NOT EXISTS category_path text,
    ADD COLUMN IF NOT EXISTS category_path_names jsonb DEFAULT '[]'::jsonb NOT NULL;

-- Index to allow searching by any ancestor token (GIN jsonb_ops)
CREATE INDEX IF NOT EXISTS idx_parts_category_path_names_gin
    ON public.parts USING gin (category_path_names jsonb_path_ops);
