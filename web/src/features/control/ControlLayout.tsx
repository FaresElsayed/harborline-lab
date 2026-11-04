import { Outlet } from 'react-router';

/** Control Center pages scroll inside the sheet with one gutter (24px) all round. */
export default function ControlLayout() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1360px] px-6 pb-10 pt-6 max-sm:px-4">
        <Outlet />
      </div>
    </div>
  );
}
