import { describe, expect, it } from 'vitest'
import { queueDesktopApproval, reduceAgentEvent, resolveApproval } from './liveState'
import type { Approval } from './types'

const approval=(id:string,tool_name='computer_click'):Approval=>({id,run_id:'run',tool_name,arguments:{},reason:'test',status:'pending',created_at:'now'})

describe('live run state',()=>{
  it('assembles streamed text, reasoning and tool activity',()=>{
    let state=reduceAgentEvent(null,{run_id:'run',type:'assistant_delta',delta:'你'})
    state=reduceAgentEvent(state,{run_id:'run',type:'assistant_delta',delta:'好'})
    state=reduceAgentEvent(state,{run_id:'run',type:'reasoning_delta',delta:'分析'})
    state=reduceAgentEvent(state,{run_id:'run',type:'tool_call',tool_call:{name:'read_file'}})
    expect(state).toMatchObject({content:'你好',reasoning:'分析',tools:['read_file'],toolActivity:[{name:'read_file',status:'running'}],status:'running'})
  })

  it('does not mix deltas from different runs',()=>{
    const first=reduceAgentEvent(null,{run_id:'one',type:'assistant_delta',delta:'old'})
    const second=reduceAgentEvent(first,{run_id:'two',type:'assistant_delta',delta:'new'})
    expect(second).toMatchObject({runId:'two',content:'new'})
  })

  it('does not restart the reply for events after completion',()=>{
    const running=reduceAgentEvent(null,{run_id:'run',type:'agent_started'})
    const completed=reduceAgentEvent(running,{run_id:'run',type:'agent_completed'})
    expect(completed).toMatchObject({runId:'run',status:'completed'})
    expect(reduceAgentEvent(completed,{run_id:'run',type:'post_run_started'})).toBe(completed)
    expect(reduceAgentEvent(null,{run_id:'run',type:'post_run_completed'})).toBeNull()
    expect(reduceAgentEvent(null,{run_id:'run',type:'agent_completed'})).toBeNull()
    expect(reduceAgentEvent(completed,{run_id:'run',type:'assistant_delta',delta:'late'})).toBe(completed)
  })
})

describe('desktop approval queue',()=>{
  it('keeps only unique desktop-scoped approvals',()=>{
    const computer=approval('a')
    const sandbox=approval('b','shell')
    expect(queueDesktopApproval([],sandbox)).toEqual([])
    expect(queueDesktopApproval(queueDesktopApproval([],computer),computer)).toEqual([computer])
  })

  it('removes resolved approvals',()=>{
    expect(resolveApproval([approval('a'),approval('b')],'a').map(item=>item.id)).toEqual(['b'])
  })
})
