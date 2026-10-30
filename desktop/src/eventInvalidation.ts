export function queryKeysForAgentEvent(kind:string):string[]{
  // 流式 token 只更新组件状态；每个 token 都回查数据库会让界面追不上输出。
  if(kind==='assistant_delta'||kind==='reasoning_delta')return []
  if(kind==='tool_call'||kind==='tool_result'||kind==='context_prepared')return ['trace']
  if(kind.startsWith('approval_'))return ['approvals','trace']
  if(kind.startsWith('post_run')||kind==='auxiliary_model_completed')return ['trace','memory','extensions']
  if(kind.startsWith('task_'))return ['tasks','trace']
  if(kind==='agent_completed'||kind==='agent_failed')return ['runs','trace','conversations','tasks','memory','extensions']
  return ['trace']
}
