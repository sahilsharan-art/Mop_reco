import {createFileRoute,redirect} from '@tanstack/react-router';
import {Screen} from '../components/Screen';
import {meta} from '../lib/meta';
import {meQuery} from '../lib/api';
export const Route=createFileRoute('/_workspace/tasks')({ssr:false,head:()=>meta('Store Task','MOP Reconciliation store task workspace with existing Python engine data.'),component:()=> <Screen kind="tasks"/>});
