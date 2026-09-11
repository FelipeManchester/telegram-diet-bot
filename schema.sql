-- Rodar uma vez no SQL Editor do Supabase.
--
-- Usa um schema dedicado pra não misturar com as tabelas dos outros projetos
-- que já vivem neste mesmo banco.
--
-- IMPORTANTE: depois de rodar isto, exponha o schema na API:
--   Dashboard > Project Settings > API > Data API > Exposed schemas
--   adicione "dieta" à lista (o default é só "public") e salve.
-- Sem esse passo o supabase-py devolve 404 na tabela.

create schema if not exists dieta;

create table if not exists dieta.refeicoes (
  id            uuid primary key default gen_random_uuid(),
  criado_em     timestamptz not null default now(),
  origem        text not null check (origem in ('texto', 'audio', 'foto')),
  entrada_bruta text,
  itens         jsonb not null,
  calorias      numeric not null,
  proteina_g    numeric not null,
  carboidrato_g numeric not null,
  gordura_g     numeric not null
);

create index if not exists refeicoes_criado_em_idx
  on dieta.refeicoes (criado_em desc);

-- A API do Supabase acessa via os roles abaixo; sem isto o PostgREST não
-- enxerga o schema mesmo depois de exposto.
grant usage on schema dieta to anon, authenticated, service_role;
grant all on all tables in schema dieta to service_role;

-- Consulta de referência pro roadmap (total por dia, no fuso de São Paulo):
--
--   select date_trunc('day', criado_em at time zone 'America/Sao_Paulo') as dia,
--          sum(calorias) as kcal,
--          sum(proteina_g) as prot,
--          sum(carboidrato_g) as carb,
--          sum(gordura_g) as gord
--     from dieta.refeicoes
--    group by dia
--    order by dia desc;
