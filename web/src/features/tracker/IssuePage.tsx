import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { useIssue } from '@/api/queries';
import { ApiError } from '@/api/client';
import { buttonClasses } from '@/components/ui/button';
import { IssueView } from './IssueView';

export default function IssuePage() {
  const { key = '' } = useParams();
  const upper = key.toUpperCase();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const q = useIssue(upper);

  if (q.error instanceof ApiError && q.error.status === 404)
    return (
      <div className="mx-auto max-w-lg px-6 py-20">
        <h1 className="figure text-[1.75rem] font-semibold text-ink">{upper} does not exist</h1>
        <p className="mt-2 text-md text-muted">Check the key, or search for the issue by its summary.</p>
        <div className="mt-6 flex gap-2">
          <Link to="/tracker/issues" className={buttonClasses('primary')}>
            Search issues
          </Link>
          <Link to="/tracker/board" className={buttonClasses()}>
            Board
          </Link>
        </div>
      </div>
    );

  return (
    <div className="h-full overflow-y-auto">
      <IssueView key={upper} issueKey={upper} variant="page" editSummary={params.get('edit') === '1'} onOpenIssue={(k) => navigate(`/tracker/issue/${k}`)} />
    </div>
  );
}
