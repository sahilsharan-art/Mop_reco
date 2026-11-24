import {createFileRoute,redirect} from '@tanstack/react-router';
import {Screen} from '../components/Screen';
import {meta} from '../lib/meta';
import {meQuery} from '../lib/api';
export const Route=createFileRoute('/_workspace/users')({ssr:false,head:()=>meta('Users','MOP Reconciliation users workspace with existing Python engine data.'),beforeLoad:async({context})=>{const u=await context.queryClient.ensureQueryData(meQuery);if(u.role!=='admin')throw redirect({to:'/summary'});},component:()=> <Screen kind="users"/>});
