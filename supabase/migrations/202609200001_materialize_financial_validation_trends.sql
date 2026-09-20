begin;

-- Materialize each selected trend once to avoid repeated payload decompression.
-- Preserve counts, manifest, controls, coordinates and ordered-hash validation.
create or replace function public.financial_validate_run(p_run_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run public.financial_runs%rowtype;
  v_period_count integer;
  v_company_count integer;
  v_source_sha256 text;
begin
  select r.* into strict v_run from public.financial_runs as r where r.id = p_run_id for update;
  select pg_catalog.count(*), pg_catalog.count(distinct p.company_code)
    into v_period_count, v_company_count
  from public.financial_periods as p where p.run_id = p_run_id;
  if v_period_count <> v_run.expected_period_count or v_company_count <> v_run.expected_company_count then
    raise exception 'run counts do not validate';
  end if;

  if exists (
    (select m.value->>'company_code' as company_code, (m.value->>'fiscal_year')::integer as fiscal_year,
            (m.value->>'fiscal_period')::integer as fiscal_period
     from pg_catalog.jsonb_array_elements(v_run.expected_manifest) as m(value)
     except
     select p.company_code, p.fiscal_year, p.fiscal_period
     from public.financial_periods as p where p.run_id = p_run_id)
    union all
    (select p.company_code, p.fiscal_year, p.fiscal_period
     from public.financial_periods as p where p.run_id = p_run_id
     except
     select m.value->>'company_code', (m.value->>'fiscal_year')::integer, (m.value->>'fiscal_period')::integer
     from pg_catalog.jsonb_array_elements(v_run.expected_manifest) as m(value))
  ) then
    raise exception 'run period keys do not exactly match expected manifest';
  end if;

  if exists (
    with trends as materialized (
      select p.fiscal_year, p.fiscal_period, p.payload#>'{statement,trend}' as trend
      from public.financial_periods as p where p.run_id = p_run_id
    )
    select 1 from trends as p
    where (
        pg_catalog.jsonb_typeof(p.trend) <> 'array'
        or pg_catalog.jsonb_array_length(p.trend) = 0
        or exists (
          select 1 from pg_catalog.jsonb_array_elements(p.trend) as t(value)
          where coalesce((t.value#>>'{controls,passed}')::boolean, false) is not true
        )
        or not exists (
          select 1
          from pg_catalog.jsonb_array_elements(p.trend) with ordinality as t(value, ordinal)
          where t.ordinal = pg_catalog.jsonb_array_length(p.trend)
            and (t.value->>'fiscal_year')::integer = p.fiscal_year
            and (t.value->>'fiscal_period')::integer = p.fiscal_period
        )
      )
  ) then
    raise exception 'period trend controls or final coordinates do not validate';
  end if;

  select pg_catalog.encode(
           extensions.digest(
             pg_catalog.convert_to(pg_catalog.string_agg(p.payload_sha256, '' order by p.company_code, p.fiscal_year, p.fiscal_period), 'UTF8'),
             'sha256'
           ), 'hex'
         ) into v_source_sha256
  from public.financial_periods as p where p.run_id = p_run_id;
  if v_source_sha256 is distinct from v_run.source_sha256 then
    raise exception 'run source hash does not validate';
  end if;

  insert into public.financial_run_events(run_id,event) values(p_run_id,'validated')
  on conflict (run_id,event) do nothing;
  return true;
end
$$;

commit;
