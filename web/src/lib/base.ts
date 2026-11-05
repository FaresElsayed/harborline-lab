/**
 * For links the router does not handle (new tabs, reloads, copied links, files next to the app). The app sits at the
 * root unless it was built with --base, as the GitHub Pages preview is.
 */
export const withBase = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\//, '')}`;

/** A handbook page, with its #anchor if it has one. */
export const handbookUrl = (page: string) => withBase(`/handbook/${page}`);

/** Where a preview visitor goes to install the real thing. */
export const REPO_URL = import.meta.env.VITE_REPO_URL || 'https://github.com/KarimAbdelaziz2503/harborline-lab';
