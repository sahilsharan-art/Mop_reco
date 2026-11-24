import {createFileRoute,redirect} from '@tanstack/react-router';
import {Screen} from '../components/Screen';
import {meta} from '../lib/meta';
import {meQuery} from '../lib/api';
export const Route=createFileRoute('/_workspace/uploads')({ssr:false,head:()=>meta('Upload','MOP Reconciliation upload workspace with existing Python engine data.'),component:()=> <Screen kind="uploads"/>});
