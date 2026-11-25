import { queryOptions } from '@tanstack/react-query';
export type Row = Record<string, unknown>;
export interface User { id: number; username: string; display_name: string; role: 'admin' | 'user'; active: boolean; must_change_password: boolean; }
export interface Bootstrap { user: User; run_id: number | null; date_from: string; date_to: string; stores: string[]; runs: Row[]; masters: { key: string; title: string }[]; sources: { key: string; title: string; extensions: string[]; month_specific: boolean; note: string }[]; }
export interface Filter { run_id: string; date_from: string; date_to: string; store: string; }
export interface TableData { rows: Row[]; total?: number; value?: number; groups?: Record<'not_received' | 'received_not_billed', Count>; summary?: Row; fields?: string[]; version?: number | null; in_database?: boolean; history?: Row[]; }
export interface MatrixColumn { sale: number; collected: number; receivable: number; pct: number | null; }
export interface Count { count: number; value: number; }
export interface Summary { matrix: Record<string, MatrixColumn | number | unknown[]>; verdict: Row; trend: Row[]; exceptions: Row[]; cash: Row; risk: Row; bills?: Record<'open' | 'auto' | 'manual' | 'total', Count>; asserted?: number; }
export interface Notice { key: string; level: 'error' | 'warning' | 'info'; title: string; text: string; }
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
export function params(filter: Filter, extra: Record<string,string | number> = {}) { const q = new URLSearchParams(); for (const [key,value] of Object.entries({...filter,...extra})) if (String(value)) q.set(key,String(value)); return q.toString(); }
function csrf() { return document.cookie.split('; ').find(x=>x.startsWith('mop_v2_csrf='))?.split('=').slice(1).join('=') ?? ''; }
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
 const headers = new Headers(options.headers);
 if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
 if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token',csrf());
 const response = await fetch('/api/mop'+path, {...options,headers,credentials:'same-origin'});
 if (!response.ok) { const body = await response.json().catch(()=>null); throw new ApiError(response.status,typeof body?.detail==='string'?body.detail:`Request failed (${response.status}).`); }
 return response.json() as Promise<T>;
}
export const meQuery = queryOptions({queryKey:['me'],queryFn:()=>api<User>('/auth/me'),retry:false,staleTime:30000});
export const bootstrapQuery = queryOptions({queryKey:['bootstrap'],queryFn:()=>api<Bootstrap>('/bootstrap'),retry:false,staleTime:30000});
// Screens do not poll: Summary / Cash / Float cost seconds to compute and used to be re-run every minute per open tab. Only a screen that
// shows something changing (a run in progress) passes a poll interval; everything else is fresh for two minutes and refreshed by Apply / a save.
export const screenQuery = (path:string, pollMs = 0) => queryOptions({queryKey:['screen',path],queryFn:()=>api<TableData | Summary>(path),retry:false,staleTime:120000,refetchInterval:pollMs||false});
export const noticesQuery = (filter: Filter) => queryOptions({queryKey:['notices',params(filter)],queryFn:()=>api<{run_id:number;notices:Notice[]}>('/notices?'+params(filter)),retry:false,staleTime:120000});
export async function download(path:string) { const response=await fetch('/api/mop'+path,{credentials:'same-origin'}); if(!response.ok) { const body=await response.json().catch(()=>null); throw new ApiError(response.status,body?.detail??'Export failed.'); } const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]??'mop-export.xlsx';a.click();URL.revokeObjectURL(url); }
export const money = (v:unknown) => typeof v==='number'?new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:2}).format(v):'—';
const MONEY_KEY = /amount|sale|received|receivable|balance|deposit|expense|value|threshold|collected|voucher|credit_note|exposure/i;
const COUNT_KEY = /(^|_)(count|records|rows|lines|bills|total|matched|unmatched|n)$|^(row_count|size_bytes|bill_lines)$/i;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const ISO_DATETIME = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;
/** DD/MM/YYYY, always (CLAUDE.md 6.14). Anything that is not exactly an ISO date or datetime is returned unchanged, so ids are never touched. */
export function displayDate(v: string): string | null {
  let m = ISO_DATE.exec(v);
  if (m) return `${m[3]}/${m[2]}/${m[1]}`;
  m = ISO_DATETIME.exec(v);
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}` : null;
}
export function display(v:unknown,key=''): string { if(v===null||v===undefined||v==='')return '—';if(typeof v==='boolean')return v?'Yes':'No';if(typeof v==='number'){ if(MONEY_KEY.test(key))return money(v); if(COUNT_KEY.test(key)&&Number.isInteger(v))return new Intl.NumberFormat('en-IN').format(v); return String(v); }if(typeof v==='string'){ const d=displayDate(v); return d ?? v; }if(typeof v==='object')return JSON.stringify(v);return String(v); }
