// Production Bun host for the built TanStack Start app. Run: bun serve.ts
// nginx routes /api/mop to the existing Python adapter, and / to this host.
import app from './dist/server/server.js';
import {resolve,sep} from 'node:path';
const root=resolve(import.meta.dir,'dist/client');
const port=Number(Bun.env.PORT??8093);
Bun.serve({hostname:'127.0.0.1',port,async fetch(request){
 const url=new URL(request.url);
 if(url.pathname.startsWith('/assets/')){
  let path:string;
  try{path=resolve(root,'.'+decodeURIComponent(url.pathname));}catch{return new Response('Bad path',{status:400});}
  if(!path.startsWith(root+sep))return new Response('Forbidden',{status:403});
  const file=Bun.file(path);
  if(!await file.exists())return new Response('Not found',{status:404});
  return new Response(file,{headers:{'Cache-Control':'public, max-age=31536000, immutable','X-Content-Type-Options':'nosniff'}});
 }
 return app.fetch(request);
}});
console.log(`MOP V2 frontend listening on http://127.0.0.1:${port}`);
