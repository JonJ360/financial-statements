begin;
set local lock_timeout='3s';
set local statement_timeout='25s';
create table public.financial_period_content (
 payload_sha256 text primary key check(payload_sha256 ~ '^[0-9a-f]{64}$'),
 payload jsonb,
 legacy_run_id uuid,
 legacy_company_code text,
 legacy_fiscal_year integer,
 legacy_fiscal_period integer,
 created_at timestamptz not null default clock_timestamp(),
 foreign key(legacy_run_id,legacy_company_code,legacy_fiscal_year,legacy_fiscal_period) references public.financial_periods(run_id,company_code,fiscal_year,fiscal_period),
 check ((payload is not null and legacy_run_id is null and legacy_company_code is null and legacy_fiscal_year is null and legacy_fiscal_period is null) or (payload is null and legacy_run_id is not null and legacy_company_code is not null and legacy_fiscal_year is not null and legacy_fiscal_period is not null))
);
create table public.financial_period_refs (
 run_id uuid not null references public.financial_runs(id),
 company_code text not null,company_name text not null,
 fiscal_year integer not null check(fiscal_year>0),fiscal_period integer not null check(fiscal_period>0),
 period_start date not null,period_end date not null check(period_end>=period_start),
 payload_sha256 text not null references public.financial_period_content(payload_sha256),
 created_at timestamptz not null default clock_timestamp(),
 primary key(run_id,company_code,fiscal_year,fiscal_period)
);
create view public.financial_periods_resolved as
 select * from public.financial_periods
 union all
 select r.run_id,r.company_code,r.company_name,r.fiscal_year,r.fiscal_period,r.period_start,r.period_end,r.payload_sha256,
 coalesce(c.payload,l.payload) payload,r.created_at
 from public.financial_period_refs r join public.financial_period_content c using(payload_sha256)
 left join public.financial_periods l on l.run_id=c.legacy_run_id and l.company_code=c.legacy_company_code and l.fiscal_year=c.legacy_fiscal_year and l.fiscal_period=c.legacy_fiscal_period;
create trigger financial_content_immutable before update or delete on public.financial_period_content for each row execute function public.financial_reject_immutable_change();
create trigger financial_refs_immutable before update or delete on public.financial_period_refs for each row execute function public.financial_reject_immutable_change();
alter table public.financial_period_content enable row level security;
alter table public.financial_period_content force row level security;
alter table public.financial_period_refs enable row level security;
alter table public.financial_period_refs force row level security;
revoke all on public.financial_period_content from "anon","ar_current_ingest","ar_current_operator","ar_current_promoter","authenticated","dashboard_user",public,"service_role";
revoke all on public.financial_period_refs from "anon","ar_current_ingest","ar_current_operator","ar_current_promoter","authenticated","dashboard_user",public,"service_role";
revoke all on public.financial_periods_resolved from "anon","ar_current_ingest","ar_current_operator","ar_current_promoter","authenticated","dashboard_user",public,"service_role";
-- Seed only the validated current run with small references to immutable legacy rows.
insert into public.financial_period_content(payload_sha256,legacy_run_id,legacy_company_code,legacy_fiscal_year,legacy_fiscal_period)
select distinct on(p.payload_sha256) p.payload_sha256,p.run_id,p.company_code,p.fiscal_year,p.fiscal_period
from public.financial_periods p join public.financial_current c on p.run_id=c.current_run_id
order by p.payload_sha256,p.company_code,p.fiscal_year,p.fiscal_period;
CREATE OR REPLACE FUNCTION public.financial_stage_period_batch(p_run_id uuid, p_periods jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
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
    insert into public.financial_period_content(payload_sha256,payload)
    select distinct on (e.value->>'payload_sha256') e.value->>'payload_sha256',e.value->'payload'
    from pg_catalog.jsonb_array_elements(p_periods) e(value)
    on conflict(payload_sha256) do nothing;
    if exists (
      select 1 from pg_catalog.jsonb_array_elements(p_periods) e(value)
      join public.financial_period_content c on c.payload_sha256=e.value->>'payload_sha256'
      left join public.financial_periods l on l.run_id=c.legacy_run_id and l.company_code=c.legacy_company_code and l.fiscal_year=c.legacy_fiscal_year and l.fiscal_period=c.legacy_fiscal_period
      where coalesce(c.payload,l.payload) is distinct from e.value->'payload'
    ) then raise exception 'content hash collision or payload mismatch'; end if;
    insert into public.financial_period_refs(run_id,company_code,company_name,fiscal_year,fiscal_period,period_start,period_end,payload_sha256)
    select p_run_id,e.value->>'company_code',e.value->>'company_name',(e.value->>'fiscal_year')::integer,(e.value->>'fiscal_period')::integer,(e.value->>'period_start')::date,(e.value->>'period_end')::date,e.value->>'payload_sha256'
    from pg_catalog.jsonb_array_elements(p_periods) e(value)
    where not exists(select 1 from public.financial_periods l where l.run_id=p_run_id and l.company_code=e.value->>'company_code' and l.fiscal_year=(e.value->>'fiscal_year')::integer and l.fiscal_period=(e.value->>'fiscal_period')::integer)
    on conflict(run_id,company_code,fiscal_year,fiscal_period) do nothing;
    get diagnostics v_inserted = row_count;
  end if;

  if exists (
    select 1
    from pg_catalog.jsonb_array_elements(p_periods) as e(value)
    left join public.financial_periods_resolved as p
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
$function$
;
CREATE OR REPLACE FUNCTION public.financial_validate_run(p_run_id uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
 v_run public.financial_runs%rowtype;
 v_period_count integer;
 v_company_count integer;
 v_source_sha256 text;
begin
 select r.* into strict v_run from public.financial_runs r where r.id=p_run_id for update;
 select pg_catalog.count(*),pg_catalog.count(distinct p.company_code) into v_period_count,v_company_count from public.financial_periods_resolved p where p.run_id=p_run_id;
 if v_period_count<>v_run.expected_period_count or v_company_count<>v_run.expected_company_count then raise exception 'run counts do not validate'; end if;
 if exists (
 (select m.value->>'company_code' as company_code,(m.value->>'fiscal_year')::integer as fiscal_year,(m.value->>'fiscal_period')::integer as fiscal_period from pg_catalog.jsonb_array_elements(v_run.expected_manifest) m(value) except select p.company_code,p.fiscal_year,p.fiscal_period from public.financial_periods_resolved p where p.run_id=p_run_id)
 union all
 (select p.company_code,p.fiscal_year,p.fiscal_period from public.financial_periods_resolved p where p.run_id=p_run_id except select m.value->>'company_code',(m.value->>'fiscal_year')::integer,(m.value->>'fiscal_period')::integer from pg_catalog.jsonb_array_elements(v_run.expected_manifest) m(value))
 ) then raise exception 'run period keys do not exactly match expected manifest'; end if;
 if exists (
 with trends as materialized (select p.fiscal_year,p.fiscal_period,p.payload#>'{statement,trend}' as trend from public.financial_periods_resolved p where p.run_id=p_run_id)
 select 1 from trends p where (
 pg_catalog.jsonb_typeof(p.trend)<>'array' or pg_catalog.jsonb_array_length(p.trend)=0
 or exists(select 1 from pg_catalog.jsonb_array_elements(p.trend) t(value) where coalesce((t.value#>>'{controls,passed}')::boolean,false) is not true)
 or not exists(select 1 from pg_catalog.jsonb_array_elements(p.trend) with ordinality t(value,ordinal) where t.ordinal=pg_catalog.jsonb_array_length(p.trend) and (t.value->>'fiscal_year')::integer=p.fiscal_year and (t.value->>'fiscal_period')::integer=p.fiscal_period)
 )) then raise exception 'period trend controls or final coordinates do not validate'; end if;
 select pg_catalog.encode(extensions.digest(pg_catalog.convert_to(pg_catalog.string_agg(p.payload_sha256,'' order by p.company_code,p.fiscal_year,p.fiscal_period),'UTF8'),'sha256'),'hex') into v_source_sha256 from public.financial_periods_resolved p where p.run_id=p_run_id;
 if v_source_sha256 is distinct from v_run.source_sha256 then raise exception 'run source hash does not validate'; end if;
 insert into public.financial_run_events(run_id,event) values(p_run_id,'validated') on conflict(run_id,event) do nothing;
 return true;
end $function$
;
CREATE OR REPLACE FUNCTION public.financial_verify_current()
 RETURNS TABLE(run_id uuid, source_sha256 text, is_current boolean, period_count bigint, previous_run_id uuid)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
begin
  return query
  select c.current_run_id, c.source_sha256,
         exists(select 1 from public.financial_run_events as e where e.run_id=c.current_run_id and e.event='promoted'),
         pg_catalog.count(p.*), c.previous_run_id
  from public.financial_current as c
  join public.financial_periods_resolved as p on p.run_id=c.current_run_id
  where c.singleton=true
  group by c.current_run_id,c.source_sha256,c.previous_run_id;
end
$function$
;
CREATE OR REPLACE FUNCTION public.financial_statement_catalog()
 RETURNS TABLE(company_code text, company_name text, fiscal_year integer, fiscal_period integer, period_start date, period_end date, period_label text)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
begin
  if auth.uid() is distinct from 'b605d98f-498e-4a94-94cf-e055ed2b5fcc'::uuid then
    raise exception 'access denied';
  end if;
  return query
  select p.company_code,p.company_name,p.fiscal_year,p.fiscal_period,p.period_start,p.period_end,
         pg_catalog.concat(p.period_start::text,' – ',p.period_end::text,' · FY ',p.fiscal_year::text,' P',p.fiscal_period::text)
  from public.financial_periods_resolved as p
  join public.financial_current as c on c.current_run_id=p.run_id
  where c.singleton=true
  order by p.company_code,p.fiscal_year desc,p.fiscal_period desc;
end
$function$
;
CREATE OR REPLACE FUNCTION public.financial_statement_period(p_company_code text, p_fiscal_year integer, p_fiscal_period integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  v_statement jsonb;
begin
  if auth.uid() is distinct from 'b605d98f-498e-4a94-94cf-e055ed2b5fcc'::uuid then
    raise exception 'access denied';
  end if;
  select p.payload into strict v_statement
  from public.financial_periods_resolved as p
  join public.financial_current as c on c.current_run_id=p.run_id
  where c.singleton=true
    and p.company_code=p_company_code
    and p.fiscal_year=p_fiscal_year
    and p.fiscal_period=p_fiscal_period;
  return v_statement;
exception
  when no_data_found then raise exception 'financial statement period not found';
end
$function$
;
commit;
