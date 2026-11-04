/**
 * For links the router does not handle (new tabs, reloads, copied links, files next to the app). The app sits at the
 * root unless it was built with --base, as the GitHub Pages demo is.
 */
export const withBase = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\//, '')}`;
