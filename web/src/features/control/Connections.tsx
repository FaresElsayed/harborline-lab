import { useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { Eye, EyeOff } from 'lucide-react';
import { useConnections } from '@/api/queries';
import type { Connections } from '@/api/types';
import { IconButton } from '@/components/ui/button';
import { CopyButton, ErrorNote, PageHeader, Skeleton } from '@/components/ui/misc';
import { Segmented } from '@/components/ui/field';
import { handbookUrl } from '@/lib/base';

const DEMO_NOTE = import.meta.env.VITE_DEMO === '1' ? ' In this demo nothing answers at these addresses: they work when you install it.' : '';

function Row({ label, value, secret, mono = true }: { label: string; value: string; secret?: boolean; mono?: boolean }) {
  const [shown, setShown] = useState(false);
  const display = secret && !shown ? '•'.repeat(Math.min(14, value.length)) : value;
  return (
    <div className="grid grid-cols-[160px_minmax(0,1fr)_auto] items-center gap-3 border-t border-rule py-1.5 first:border-t-0 max-sm:grid-cols-[1fr_auto]">
      <dt className="text-base text-muted max-sm:col-span-2">{label}</dt>
      <dd className={mono ? 'min-w-0 truncate font-mono text-[13px] text-ink' : 'min-w-0 truncate text-base text-ink'}>{display}</dd>
      <div className="flex items-center">
        {secret ? (
          <IconButton label={shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`} size="sm" onClick={() => setShown((v) => !v)}>
            {shown ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
          </IconButton>
        ) : null}
        <CopyButton text={value} label={`Copy ${label.toLowerCase()}`} toastLabel={`${label} copied`} />
      </div>
    </div>
  );
}

function Snippet({ code, label }: { code: string; label: string }) {
  return (
    <div className="relative mt-3 rounded-m border border-line bg-raised">
      <div className="flex items-center justify-between border-b border-rule py-1 pl-3 pr-1">
        <span className="text-sm text-muted">{label}</span>
        <CopyButton text={code} label={`Copy ${label}`} toastLabel="Snippet copied" />
      </div>
      <pre className="scroll-thin overflow-x-auto px-3 py-2.5 font-mono text-[12.5px] leading-5 text-ink">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside: ReactNode }) {
  return (
    <section className="grid grid-cols-[minmax(0,4fr)_minmax(0,8fr)] gap-8 border-t border-line py-7 first:border-t-0 first:pt-2 max-[1000px]:grid-cols-1 max-[1000px]:gap-3">
      <div>
        <h2 className="text-md font-semibold text-ink">{title}</h2>
        <div className="mt-1 text-base text-muted">{aside}</div>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

type Shell = 'powershell' | 'bash';

function snippets(c: Connections, shell: Shell) {
  const pg = c.postgres;
  const db = pg.databases[0] ?? 'wms';
  const uri = `postgresql://${pg.user}:${pg.password}@${pg.host}:${pg.port}/${db}`;
  const sf = c.salesforce;
  const ps = shell === 'powershell';
  return {
    jdbc: `jdbc:postgresql://${pg.host}:${pg.port}/${db}`,
    uri,
    psql: ps
      ? `$env:PGPASSWORD = "${pg.password}"\npsql -h ${pg.host} -p ${pg.port} -U ${pg.user} -d ${db} -c "select count(*) from wms.order_header"`
      : `PGPASSWORD='${pg.password}' psql -h ${pg.host} -p ${pg.port} -U ${pg.user} -d ${db} -c "select count(*) from wms.order_header"`,
    sfToken: ps
      ? `$token = Invoke-RestMethod -Method Post -Uri "${sf.token_url}" \`\n  -Body @{ grant_type = "client_credentials"; client_id = "${sf.client_id}"; client_secret = "${sf.client_secret}" }\nInvoke-RestMethod -Uri "${sf.instance_url}/services/data/${sf.api_version}/query?q=SELECT+Id,Name+FROM+Account+LIMIT+5" \`\n  -Headers @{ Authorization = "Bearer $($token.access_token)" }`
      : `TOKEN=$(curl -s -X POST "${sf.token_url}" \\\n  -d grant_type=client_credentials -d client_id=${sf.client_id} -d client_secret=${sf.client_secret} | jq -r .access_token)\ncurl -s "${sf.instance_url}/services/data/${sf.api_version}/query?q=SELECT+Id,Name+FROM+Account+LIMIT+5" \\\n  -H "Authorization: Bearer $TOKEN"`,
    carrier: ps
      ? `Invoke-RestMethod -Uri "${c.carrier.base_url}/tracking-events?limit=100" -Headers @{ "X-API-Key" = "${c.carrier.api_key}" }`
      : `curl -s "${c.carrier.base_url}/tracking-events?limit=100" -H "X-API-Key: ${c.carrier.api_key}"`,
  };
}

export default function ConnectionsPage() {
  const q = useConnections();
  const [shell, setShell] = useState<Shell>('powershell');
  const c = q.data;
  const s = c ? snippets(c, shell) : null;

  return (
    <>
      <PageHeader
        title="Connections"
        subtitle={`Everything your pipelines need to reach the sources. Postgres is read-only for this user and has the REPLICATION attribute for CDC; Salesforce and the carrier feed are APIs only.${DEMO_NOTE}`}
        actions={
          <Segmented
            ariaLabel="Snippet shell"
            value={shell}
            onValueChange={setShell}
            options={[
              { value: 'powershell', label: 'PowerShell' },
              { value: 'bash', label: 'bash' },
            ]}
          />
        }
      />
      <p className="-mt-2 mb-4 text-base text-muted">
        First time?{' '}
        <a href={handbookUrl('getting-started.html#connect')} target="_blank" rel="noreferrer" className="font-medium text-accent-strong hover:underline">
          Try each source system
        </a>{' '}
        walks you through a first query.{' '}
        <a href={handbookUrl('source-systems.html')} target="_blank" rel="noreferrer" className="font-medium text-accent-strong hover:underline">
          Source systems
        </a>{' '}
        says how each one behaves. Can’t connect? Ask{' '}
        <Link to="/team/kevin-ohara" className="font-medium text-accent-strong hover:underline">
          Kevin O’Hara
        </Link>
        .
      </p>
      {q.isError ? <ErrorNote error={q.error} onRetry={() => void q.refetch()} what="connection details" /> : null}
      {q.isLoading ? (
        <div className="flex flex-col gap-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      ) : null}
      {c && s ? (
        <div className="max-w-[1120px]">
          <Section
            title="Postgres"
            aside={
              <>
                WMS, AX 2012 and IoT live in separate databases. Logical replication is on: publications <code className="font-mono text-[12.5px] text-ink">hl_wms_cdc</code>,{' '}
                <code className="font-mono text-[12.5px] text-ink">hl_ax_cdc</code> and <code className="font-mono text-[12.5px] text-ink">hl_iot_cdc</code>.
              </>
            }
          >
            <dl>
              <Row label="Host" value={c.postgres.host} />
              <Row label="Port" value={String(c.postgres.port)} />
              <Row label="User" value={c.postgres.user} />
              <Row label="Password" value={c.postgres.password} secret />
              <Row label="Databases" value={c.postgres.databases.join(', ')} />
              <Row label="JDBC URL" value={s.jdbc} />
            </dl>
            <Snippet label={shell === 'powershell' ? 'psql from PowerShell' : 'psql'} code={s.psql} />
          </Section>

          <Section title="Salesforce (mock API)" aside="OAuth client credentials, SOQL query with 2,000-row pages, Bulk API 2.0, and a 15,000-call daily limit you can run out of.">
            <dl>
              <Row label="Instance URL" value={c.salesforce.instance_url} />
              <Row label="Token URL" value={c.salesforce.token_url} />
              <Row label="Client ID" value={c.salesforce.client_id} />
              <Row label="Client secret" value={c.salesforce.client_secret} secret />
              <Row label="API version" value={c.salesforce.api_version} />
            </dl>
            <Snippet label="Get a token, then query" code={s.sfToken} />
          </Section>

          <Section title="Carrier tracking (mock API)" aside="Cursor paging, an API key header, 429 with Retry-After past 120 requests a minute, and the odd 503.">
            <dl>
              <Row label="Base URL" value={c.carrier.base_url} />
              <Row label="API key" value={c.carrier.api_key} secret />
            </dl>
            <Snippet label="First page of events" code={s.carrier} />
          </Section>

          <Section title="Landing folder" aside="EDI files and business files arrive here as the simulator runs. New versions appear over time; old ones are not removed.">
            <dl>
              <Row label="On your machine" value={c.landing_path_host_hint} />
              <Row label="EDI" value={`${c.landing_path_host_hint.replace(/[\\/]$/, '')}${c.landing_path_host_hint.includes('\\') ? '\\' : '/'}edi`} />
              <Row label="Business files" value={`${c.landing_path_host_hint.replace(/[\\/]$/, '')}${c.landing_path_host_hint.includes('\\') ? '\\' : '/'}files`} />
            </dl>
          </Section>
        </div>
      ) : null}
    </>
  );
}
