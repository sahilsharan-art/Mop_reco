import {createFileRoute,redirect} from '@tanstack/react-router';
import {meta} from '../lib/meta';
export const Route=createFileRoute('/')({head:()=>meta('Reconciliation workspace','Store collections, exceptions, cash position and reconciliation operations.'),beforeLoad:()=>{throw redirect({to:'/summary'});}});
