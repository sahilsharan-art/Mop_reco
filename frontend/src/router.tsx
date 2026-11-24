import { createRouter, type ErrorComponentProps } from '@tanstack/react-router';
import { QueryClient } from '@tanstack/react-query';
import { routeTree } from './routeTree.gen';
export function getRouter() { const queryClient=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});return createRouter({routeTree,context:{queryClient},scrollRestoration:true,defaultErrorComponent:ErrorView}); }
function ErrorView({error,reset}:ErrorComponentProps) {return <main className="empty-state"><h1>Unable to open this page</h1><p>{error instanceof Error?error.message:'Please try again.'}</p><button onClick={reset}>Try again</button><a href="/login">Sign in</a></main>;}
declare module '@tanstack/react-router' { interface Register { router: ReturnType<typeof getRouter>; } }
