class CompanionFocusRuntimeController {
  constructor({ readSession, observe, onWarning, onComplete, onError = () => {}, isGameQuiet = () => false, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 4000 }) {
    for (const [name,value] of Object.entries({readSession,observe,onWarning,onComplete,onError,isGameQuiet,setIntervalFn,clearIntervalFn})) if (typeof value!=="function") throw new TypeError(`focus ${name} handler is required`);
    if (!Number.isInteger(intervalMs)||intervalMs<3000||intervalMs>5000) throw new TypeError("focus interval is invalid");
    Object.assign(this,{readSession,observe,onWarning,onComplete,onError,isGameQuiet,setIntervalFn,clearIntervalFn,intervalMs}); this.timer=null; this.inFlight=false; this.suspended=false; this.suspendPending=false; this.locked=false; this.active=false; this.completed=new Set();
  }
  start(){if(this.timer!==null)return;this.timer=this.setIntervalFn(()=>void this.poll(),this.intervalMs);void this.poll();}
  stop(){if(this.timer!==null)this.clearIntervalFn(this.timer);this.timer=null;}
  async poll(){if(this.inFlight)return;this.inFlight=true;try{const current=await this.readSession();this.active=Boolean(current&&["running","paused"].includes(current.status));if(!this.active)return;const flags={locked:this.locked,sleeping:this.suspended,game_quiet:this.isGameQuiet()===true};const session=await this.observe(flags);this.active=["running","paused"].includes(session.status);if(flags.sleeping){this.suspendPending=false;this.suspended=false;}if(session.should_warn)this.onWarning(session);if(session.status==="completed"&&!this.completed.has(session.session_id)){this.completed.add(session.session_id);this.onComplete(session);}}catch(error){this.onError(error);}finally{this.inFlight=false;}}
  isActive(){return this.active;}
  setLocked(value){this.locked=value===true;if(this.locked)void this.poll();}
  setSuspended(value){if(value===true){this.suspended=true;this.suspendPending=true;}else if(!this.suspendPending)this.suspended=false;void this.poll();}
}
module.exports={CompanionFocusRuntimeController};
