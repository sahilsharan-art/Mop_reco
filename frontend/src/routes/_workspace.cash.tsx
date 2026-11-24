import {createFileRoute,redirect} from '@tanstack/react-router';
import {Screen} from '../components/Screen';
import {meta} from '../lib/meta';
import {meQuery} from '../lib/api';
export const Route=createFileRoute('/_workspace/cash')({ssr:false,head:()=>meta('Cash Position','MOP Reconciliation cash position workspace with existing Python engine data.'),component:()=> <Screen kind="cash"/>});
