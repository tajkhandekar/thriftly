-- hybrid_search() : keyword + semantic, fused with weighted RRF.
-- Adapted from the Supabase hybrid-search template to the listings schema.

create or replace function hybrid_search(
  query_text       text,
  query_embedding  vector(1536),
  match_count      int   = 5,
  full_text_weight float = 1,
  semantic_weight  float = 1,
  rrf_k            int   = 50
)
returns setof listings
language sql
as $$
with full_text as (
  select
    id,
    row_number() over (
      order by ts_rank_cd(listing_tsv, websearch_to_tsquery('english', query_text)) desc
    ) as rank_ix
  from listings
  where is_active
    and listing_tsv @@ websearch_to_tsquery('english', query_text)
  order by rank_ix
  limit least(match_count, 30) * 2
),
semantic as (
  select id, row_number() over (order by dist) as rank_ix
  from (
    select id, listing_embedding <=> query_embedding as dist
    from listings
    where is_active
      and listing_embedding is not null
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