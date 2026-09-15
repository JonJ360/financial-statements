begin;

create extension if not exists pgcrypto with schema extensions;

create table public.financial_runs (
  id uuid primary key default extensions.gen_random_uuid(),
  source_sha256 text not null
    check (source_sha256 OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'),
  as_of date not null,
  expected_company_count integer not null check (expected_company_count > 0),
  expected_period_count integer not null check (expected_period_count > 0),
  expected_manifest jsonb not null check (pg_catalog.jsonb_typeof(expected_manifest) = 'array'),
  created_at timestamptz not null default pg_catalog.clock_timestamp(),
  unique (source_sha256)
);

create table public.financial_periods (
  run_id uuid not null references public.financial_runs(id),
  company_code text not null,
  company_name text not null,
  fiscal_year integer not null check (fiscal_year > 0),
  fiscal_period integer not null check (fiscal_period > 0),
  period_start date not null,
  period_end date not null check (period_end >= period_start),
  payload_sha256 text not null check (payload_sha256 OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'),
  payload jsonb not null,
  created_at timestamptz not null default pg_catalog.clock_timestamp(),
  primary key (run_id, company_code, fiscal_year, fiscal_period),
  check (payload->>'company_code' = company_code),
  check (payload->>'company_name' = company_name),
  check ((payload->>'fiscal_year')::integer = fiscal_year),
  check ((payload->>'fiscal_period')::integer = fiscal_period),
  check ((payload->>'period_start')::date = period_start),
  check ((payload->>'period_end')::date = period_end)
);

create table public.financial_current (
  singleton boolean primary key default true check (singleton),
  current_run_id uuid not null unique references public.financial_runs(id),
  previous_run_id uuid unique references public.financial_runs(id),
  source_sha256 text not null,
  promoted_at timestamptz not null default pg_catalog.clock_timestamp(),
  check (previous_run_id is null or previous_run_id <> current_run_id)
);

create table public.financial_run_events (
  run_id uuid not null references public.financial_runs(id),
  event text not null check (event in ('validated','promoted','rolled_back')),
  occurred_at timestamptz not null default pg_catalog.clock_timestamp(),
  primary key (run_id,event)
);

create or replace function public.financial_reject_immutable_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  raise exception 'financial immutable rows cannot be updated or deleted';
end
$$;

create trigger financial_runs_immutable before update or delete on public.financial_runs
for each row execute function public.financial_reject_immutable_change();
create trigger financial_periods_immutable before update or delete on public.financial_periods
for each row execute function public.financial_reject_immutable_change();
create trigger financial_run_events_immutable before update or delete on public.financial_run_events
for each row execute function public.financial_reject_immutable_change();

alter table public.financial_runs enable row level security;
alter table public.financial_runs force row level security;
alter table public.financial_periods enable row level security;
alter table public.financial_periods force row level security;
alter table public.financial_current enable row level security;
alter table public.financial_current force row level security;
alter table public.financial_run_events enable row level security;
alter table public.financial_run_events force row level security;

revoke all on table public.financial_runs from public, anon, authenticated, service_role, ar_current_ingest, ar_current_promoter, ar_current_operator;
revoke all on table public.financial_periods from public, anon, authenticated, service_role, ar_current_ingest, ar_current_promoter, ar_current_operator;
revoke all on table public.financial_current from public, anon, authenticated, service_role, ar_current_ingest, ar_current_promoter, ar_current_operator;
revoke all on table public.financial_run_events from public, anon, authenticated, service_role, ar_current_ingest, ar_current_promoter, ar_current_operator;

create or replace function public.financial_stage_run(
  p_source_sha256 text,
  p_as_of date,
  p_company_count integer,
  p_period_count integer,
  p_manifest jsonb
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_id uuid;
  v_manifest_count integer;
  v_manifest_company_count integer;
begin
  if p_source_sha256 is null or p_source_sha256 OPERATOR(pg_catalog.!~) '^[0-9a-f]{64}$'
     or p_company_count <= 0 or p_period_count <= 0
     or pg_catalog.jsonb_typeof(p_manifest) <> 'array' then
    raise exception 'invalid run metadata';
  end if;

  select pg_catalog.count(*), pg_catalog.count(distinct m.value->>'company_code')
    into v_manifest_count, v_manifest_company_count
  from pg_catalog.jsonb_array_elements(p_manifest) as m(value);
  if v_manifest_count <> p_period_count or v_manifest_company_count <> p_company_count
     or exists (
       select 1
       from pg_catalog.jsonb_array_elements(p_manifest) as m(value)
       where m.value->>'company_code' is null
          or (m.value->>'fiscal_year')::integer <= 0
          or (m.value->>'fiscal_period')::integer <= 0
     )
     or exists (
       select 1
       from pg_catalog.jsonb_array_elements(p_manifest) as m(value)
       group by m.value->>'company_code', (m.value->>'fiscal_year')::integer, (m.value->>'fiscal_period')::integer
       having pg_catalog.count(*) > 1
     ) then
    raise exception 'run manifest does not match declared counts or contains duplicate/invalid keys';
  end if;

  insert into public.financial_runs(
    source_sha256, as_of, expected_company_count, expected_period_count, expected_manifest
  ) values (
    p_source_sha256, p_as_of, p_company_count, p_period_count, p_manifest
  ) on conflict (source_sha256) do nothing
  returning id into v_id;

  if v_id is null then
    select r.id into strict v_id
    from public.financial_runs as r
    where r.source_sha256 = p_source_sha256;
    if not exists (
      select 1 from public.financial_runs as r
      where r.id = v_id
        and r.as_of = p_as_of
        and r.expected_company_count = p_company_count
        and r.expected_period_count = p_period_count
        and r.expected_manifest = p_manifest
    ) then
      raise exception 'source hash already exists with different metadata';
    end if;
  end if;
  return v_id;
end
$$;

create or replace function public.financial_stage_period_batch(p_run_id uuid, p_periods jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run public.financial_runs%rowtype;
  v_input_count integer;
  v_inserted integer := 0;
  v_closed boolean;
begin
  if pg_catalog.jsonb_typeof(p_periods) <> 'array' or pg_catalog.jsonb_array_length(p_periods) = 0 then
    raise exception 'period batch must be a non-empty array';
  end if;
  select r.* into strict v_run from public.financial_runs as r where r.id = p_run_id for update;

  select pg_catalog.count(*) into v_input_count
  from pg_catalog.jsonb_array_elements(p_periods) as e(value);
  if exists (
    select 1
    from pg_catalog.jsonb_array_elements(p_periods) as e(value)
    group by e.value->>'company_code', (e.value->>'fiscal_year')::integer, (e.value->>'fiscal_period')::integer
    having pg_catalog.count(*) > 1
  ) then
    raise exception 'period batch contains duplicate input coordinates';
  end if;

  if exists (
    select 1 from pg_catalog.jsonb_array_elements(p_periods) as e(value)
    where e.value->>'payload_sha256' is null
       or e.value->>'payload_sha256' OPERATOR(pg_catalog.!~) '^[0-9a-f]{64}$'
       or e.value->>'payload_canonical' is null
       or (e.value->>'payload_canonical')::jsonb is distinct from e.value->'payload'
       or pg_catalog.encode(extensions.digest(pg_catalog.convert_to(e.value->>'payload_canonical', 'UTF8'), 'sha256'), 'hex') <> e.value->>'payload_sha256'
       or e.value->>'company_code' is distinct from e.value#>>'{payload,company_code}'
       or e.value->>'company_name' is distinct from e.value#>>'{payload,company_name}'
       or (e.value->>'fiscal_year')::integer is distinct from (e.value#>>'{payload,fiscal_year}')::integer
       or (e.value->>'fiscal_period')::integer is distinct from (e.value#>>'{payload,fiscal_period}')::integer
       or (e.value->>'period_start')::date is distinct from (e.value#>>'{payload,period_start}')::date
       or (e.value->>'period_end')::date is distinct from (e.value#>>'{payload,period_end}')::date
  ) then
    raise exception 'period envelope payload integrity or metadata validation failed';
  end if;

  select exists(select 1 from public.financial_run_events as ev where ev.run_id = p_run_id) into v_closed;
  if not v_closed then
    insert into public.financial_periods(
      run_id, company_code, company_name, fiscal_year, fiscal_period,
      period_start, period_end, payload_sha256, payload
    )
    select p_run_id, e.value->>'company_code', e.value->>'company_name',
           (e.value->>'fiscal_year')::integer, (e.value->>'fiscal_period')::integer,
           (e.value->>'period_start')::date, (e.value->>'period_end')::date,
           e.value->>'payload_sha256', e.value->'payload'
    from pg_catalog.jsonb_array_elements(p_periods) as e(value)
    on conflict (run_id, company_code, fiscal_year, fiscal_period) do nothing;
    get diagnostics v_inserted = row_count;
  end if;

  if exists (
    select 1
    from pg_catalog.jsonb_array_elements(p_periods) as e(value)
    left join public.financial_periods as p
      on p.run_id = p_run_id
     and p.company_code = e.value->>'company_code'
     and p.fiscal_year = (e.value->>'fiscal_year')::integer
     and p.fiscal_period = (e.value->>'fiscal_period')::integer
    where p.run_id is null
       or p.company_name is distinct from e.value->>'company_name'
       or p.period_start is distinct from (e.value->>'period_start')::date
       or p.period_end is distinct from (e.value->>'period_end')::date
       or p.payload_sha256 is distinct from e.value->>'payload_sha256'
       or p.payload is distinct from e.value->'payload'
  ) then
    raise exception 'period coordinate conflict does not exactly match staged data';
  end if;
  return v_inserted;
end
$$;

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
    select 1 from public.financial_periods as p
    where pg_catalog.jsonb_typeof(p.payload#>'{statement,trend}') <> 'array'
       or pg_catalog.jsonb_array_length(p.payload#>'{statement,trend}') = 0
       or exists (
         select 1 from pg_catalog.jsonb_array_elements(p.payload#>'{statement,trend}') as t(value)
         where coalesce((t.value#>>'{controls,passed}')::boolean, false) is not true
       )
       or not exists (
         select 1
         from pg_catalog.jsonb_array_elements(p.payload#>'{statement,trend}') with ordinality as t(value, ordinal)
         where t.ordinal = pg_catalog.jsonb_array_length(p.payload#>'{statement,trend}')
           and (t.value->>'fiscal_year')::integer = p.fiscal_year
           and (t.value->>'fiscal_period')::integer = p.fiscal_period
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

create or replace function public.financial_promote_run(p_run_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run public.financial_runs%rowtype;
  v_current_run_id uuid;
begin
  select r.* into strict v_run from public.financial_runs as r where r.id = p_run_id for update;
  if not exists(select 1 from public.financial_run_events as e where e.run_id = p_run_id and e.event = 'validated') then
    raise exception 'run is not validated';
  end if;
  select c.current_run_id into v_current_run_id
  from public.financial_current as c where c.singleton = true for update;
  if v_current_run_id = p_run_id then
    insert into public.financial_run_events(run_id,event) values(p_run_id,'promoted')
    on conflict (run_id,event) do nothing;
    return true;
  end if;
  if exists(select 1 from public.financial_run_events as e where e.run_id = p_run_id and e.event = 'promoted') then
    raise exception 'run was previously promoted and is no longer current';
  end if;
  insert into public.financial_current(singleton,current_run_id,previous_run_id,source_sha256,promoted_at)
  values(true,p_run_id,v_current_run_id,v_run.source_sha256,pg_catalog.clock_timestamp())
  on conflict(singleton) do update
    set current_run_id=excluded.current_run_id,
        previous_run_id=public.financial_current.current_run_id,
        source_sha256=excluded.source_sha256,
        promoted_at=excluded.promoted_at;
  insert into public.financial_run_events(run_id,event) values(p_run_id,'promoted')
  on conflict (run_id,event) do nothing;
  return true;
end
$$;

create or replace function public.financial_rollback_current(p_expected_current_run_id uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_current uuid;
  v_previous uuid;
  v_source text;
begin
  select c.current_run_id, c.previous_run_id into strict v_current, v_previous
  from public.financial_current as c where c.singleton = true for update;
  if v_current <> p_expected_current_run_id then
    if v_previous = p_expected_current_run_id then
      return v_current;
    end if;
    raise exception 'current run does not match rollback precondition';
  end if;
  if v_previous is null then raise exception 'no previous financial run is available'; end if;
  select r.source_sha256 into strict v_source from public.financial_runs as r where r.id = v_previous;
  update public.financial_current
     set current_run_id = v_previous, previous_run_id = v_current,
         source_sha256 = v_source, promoted_at = pg_catalog.clock_timestamp()
   where singleton = true;
  insert into public.financial_run_events(run_id,event) values(v_current,'rolled_back')
  on conflict (run_id,event) do nothing;
  return v_previous;
end
$$;

create or replace function public.financial_verify_current()
returns table(run_id uuid,source_sha256 text,is_current boolean,period_count bigint,previous_run_id uuid)
language plpgsql
security definer
set search_path = ''
as $$
begin
  return query
  select c.current_run_id, c.source_sha256,
         exists(select 1 from public.financial_run_events as e where e.run_id=c.current_run_id and e.event='promoted'),
         pg_catalog.count(p.*), c.previous_run_id
  from public.financial_current as c
  join public.financial_periods as p on p.run_id=c.current_run_id
  where c.singleton=true
  group by c.current_run_id,c.source_sha256,c.previous_run_id;
end
$$;

create or replace function public.financial_statement_catalog()
returns table(
  company_code text, company_name text, fiscal_year integer, fiscal_period integer,
  period_start date, period_end date, period_label text
)
language plpgsql
security definer
set search_path = ''
as $$
begin
  if auth.uid() is distinct from 'b605d98f-498e-4a94-94cf-e055ed2b5fcc'::uuid then
    raise exception 'access denied';
  end if;
  return query
  select p.company_code,p.company_name,p.fiscal_year,p.fiscal_period,p.period_start,p.period_end,
         pg_catalog.concat(p.period_start::text,' – ',p.period_end::text,' · FY ',p.fiscal_year::text,' P',p.fiscal_period::text)
  from public.financial_periods as p
  join public.financial_current as c on c.current_run_id=p.run_id
  where c.singleton=true
  order by p.company_code,p.fiscal_year desc,p.fiscal_period desc;
end
$$;

create or replace function public.financial_statement_period(
  p_company_code text,
  p_fiscal_year integer,
  p_fiscal_period integer
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_statement jsonb;
begin
  if auth.uid() is distinct from 'b605d98f-498e-4a94-94cf-e055ed2b5fcc'::uuid then
    raise exception 'access denied';
  end if;
  select p.payload into strict v_statement
  from public.financial_periods as p
  join public.financial_current as c on c.current_run_id=p.run_id
  where c.singleton=true
    and p.company_code=p_company_code
    and p.fiscal_year=p_fiscal_year
    and p.fiscal_period=p_fiscal_period;
  return v_statement;
exception
  when no_data_found then raise exception 'financial statement period not found';
end
$$;

revoke all on function public.financial_reject_immutable_change() from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_stage_run(text,date,integer,integer,jsonb) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_stage_period_batch(uuid,jsonb) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_validate_run(uuid) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_promote_run(uuid) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_rollback_current(uuid) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_verify_current() from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_statement_catalog() from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;
revoke all on function public.financial_statement_period(text,integer,integer) from public,anon,authenticated,service_role,ar_current_ingest,ar_current_promoter,ar_current_operator;

grant execute on function public.financial_stage_run(text,date,integer,integer,jsonb), public.financial_stage_period_batch(uuid,jsonb), public.financial_validate_run(uuid) to ar_current_ingest;
grant execute on function public.financial_promote_run(uuid), public.financial_rollback_current(uuid) to ar_current_promoter;
grant execute on function public.financial_verify_current() to ar_current_operator;
grant execute on function public.financial_statement_catalog(), public.financial_statement_period(text,integer,integer) to authenticated;

commit;
