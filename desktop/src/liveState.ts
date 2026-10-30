import type { Approval } from './types'

export type LiveTool={id:string;name:string;status:'running'|'completed'|'failed';summary?:string}
export type LiveRunState={runId:string;content:string;reasoning:string;tools:string[];toolActivity:LiveTool[];status:string;budgetStatus?:string}

// 把流式事件折叠为当前 Run 的界面状态；历史事件不应污染新回复。
export function reduceAgentEvent(previous:LiveRunState|null,event:any):LiveRunState|null{
  const type=String(event?.type||'')
  const runId=String(event?.run_id||'')
  const isLiveEvent=['agent_started','assistant_delta','reasoning_delta','tool_call','tool_result','agent_completed','agent_failed'].includes(type)||type.startsWith('run_budget_')
  // 运行结束后的反思和追踪事件不属于新的实时回复。
  if(!isLiveEvent||!runId)return previous
  if(type==='agent_completed'||type==='agent_failed'){
    if(!previous||previous.runId!==runId)return previous
    return{...previous,status:type==='agent_completed'?'completed':'failed'}
  }
  if(previous?.runId===runId&&['completed','failed','interrupted'].includes(previous.status))return previous
  // 收到另一个 Run 的事件时，新建独立的实时缓冲区。
  const current:LiveRunState=previous&&previous.runId===runId
    ? previous
    : {runId,content:'',reasoning:'',tools:[],toolActivity:[],status:'running'}
  if(type==='assistant_delta')return{...current,content:current.content+String(event.delta||'')}
  if(type==='reasoning_delta')return{...current,reasoning:current.reasoning+String(event.delta||'')}
  if(type==='tool_call'){
    const name=String(event.tool_call?.name||'tool')
    const id=String(event.tool_call?.id||`${name}:${current.toolActivity.length}`)
    return{...current,tools:[...current.tools,name],toolActivity:[...current.toolActivity,{id,name,status:'running'}]}
  }
  if(type==='tool_result'){
    const id=String(event.tool_result?.tool_call_id||event.tool_call_id||'')
    const success=event.tool_result?.success!==false
    // 按调用 ID 匹配结果，因为同一 Run 可以并发调用同名工具。
    return{...current,toolActivity:current.toolActivity.map(item=>item.id===id?{...item,status:success?'completed':'failed',summary:String(event.tool_result?.error||event.tool_result?.output||'').slice(0,160)}:item)}
  }
  if(type.startsWith('run_budget_'))return{...current,budgetStatus:type.replace('run_budget_','')}
  return current
}

export function queueDesktopApproval(queue:Approval[],approval:Approval):Approval[]{
  // 电脑操作审批也会显示在独立桌面窗口中。
  const isDesktop=approval.ui_scope==='desktop'||approval.tool_name.startsWith('computer_')
  if(!isDesktop||queue.some(item=>item.id===approval.id))return queue
  return[...queue,approval]
}

export function resolveApproval(queue:Approval[],approvalId:string):Approval[]{
  return queue.filter(item=>item.id!==approvalId)
}
