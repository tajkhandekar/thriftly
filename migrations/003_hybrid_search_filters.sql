-- hybrid_search() with hard filters (price range, condition, source).
--
-- Filters are applied inside BOTH the keyword and semantic candidate sets,
-- before ranking, so filtered-out listings never take up a top-k slot.
-- NULL (or an empty array) for a filter means "no constraint".
-- conditions match case-insensitively by prefix, because eBay grades them:
-- 'Pre-owned' matches 'Pre-owned - Good' / '- Excellent', 'New' matches
-- 'New with tags' / 'New without box', and an exact value still matches itself.
--
-- hnsw.iterative_scan (pgvector >= 0.8): when a filter removes most of the
-- nearest neighbours, keep scanning the HNSW index instead of returning too
-- few rows. relaxed_order is fine because we re-rank by distance below.

-- Load pgvector's shared library in this session (casting a literal does it)
-- so hnsw.iterative_scan is a registered setting. Otherwise a non-superuser
-- (e.g. on RDS) gets "permission denied to set parameter" on the SET clause.
select '[1]'::vector;

-- New parameters change the signature, so drop the old one instead of
-- leaving an overload behind.
drop function if exists hybrid_search(text, vector, int, float, float, int);

create or replace function hybrid_search(
  query_text       text,
  query_embedding  vector(1536),
  match_count      int    = 5,
  full_text_weight float  = 1,
  semantic_weight  float  = 1,
  rrf_k            int    = 50,
  min_price_cents  int    = null,
  max_price_cents  int    = null,
  conditions       text[] = null,
  sources          text[] = null
)
returns setof listings
language sql
stable
set hnsw.iterative_scan = 'relaxed_order'
as $$
-- NOT MATERIALIZED: filtered is referenced twice, and a materialized CTE
-- would hide the GIN and HNSW indexes from both branches.
with filtered as not materialized (
  select l.*
  from listings l
  where l.is_active
    and (min_price_cents is null or l.price_cents >= min_price_cents)
    and (max_price_cents is null or l.price_cents <= max_price_cents)
    and (coalesce(cardinality(conditions), 0) = 0
         or exists (select 1 from unnest(conditions) c
                    where starts_with(lower(l.condition), lower(c))))
    and (coalesce(cardinality(sources), 0) = 0 or l.source = any (sources))
),
full_text as (
  select
    id,
    row_number() over (
      order by ts_rank_cd(listing_tsv, websearch_to_tsquery('english', query_text)) desc
    ) as rank_ix
  from filtered
  where listing_tsv @@ websearch_to_tsquery('english', query_text)
  order by rank_ix
  limit least(match_count, 30) * 2
),
semantic as (
  select id, row_number() over (order by dist) as rank_ix
  from (
    select id, listing_embedding <=> query_embedding as dist
    from filtered
    where listing_embedding is not null
    order by listing_embedding <=> query_embedding   -- HNSW top-k
    limit least(match_count, 30) * 2
  ) nn
)
select listings.*
from full_text
  full outer join semantic on full_text.id = semantic.id
  join listings on coalesce(full_text.id, semantic.id) = listings.id
order by
  coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
  coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight
  desc
limit least(match_count, 30);
$$;
