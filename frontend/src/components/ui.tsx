import {useEffect,useRef,type ButtonHTMLAttributes,type ReactNode} from 'react';
import {X,LoaderCircle} from 'lucide-react';
export function Button({variant='primary',busy=false,children,className='',...props}:ButtonHTMLAttributes<HTMLButtonElement>&{variant?:'primary'|'secondary'|'danger'|'icon';busy?:boolean}){return <button className={`button button-${variant} ${className}`} {...props} disabled={busy||props.disabled}>{busy?<LoaderCircle className="spin" size={16}/>:null}{children}</button>;}
export function Modal({title,children,onClose}:{title:string;children:ReactNode;onClose:()=>void}) {
 const ref=useRef<HTMLDivElement>(null);
 useEffect(()=>{const previous=document.activeElement;const root=ref.current;const oldOverflow=document.body.style.overflow;document.body.style.overflow='hidden';root?.querySelector<HTMLElement>('button,input,select,textarea')?.focus();
 function key(e:KeyboardEvent){if(e.key==='Escape'){e.preventDefault();onClose();}if(e.key==='Tab'){const nodes=Array.from(root?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')??[]);const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}}
 document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);document.body.style.overflow=oldOverflow;if(previous instanceof HTMLElement)previous.focus();};},[onClose]);
 return <div ref={ref} className="modal-backdrop" onClick={onClose}><section className="modal" role="dialog" aria-modal="true" aria-label={title} onClick={e=>e.stopPropagation()}><header><h2>{title}</h2><Button variant="icon" aria-label="Close dialog" onClick={onClose}><X size={18}/></Button></header>{children}</section></div>;
}
export function Pill({children,tone='neutral'}:{children:ReactNode;tone?:string}) {return <span className={`pill pill-${tone}`}>{children}</span>;}
