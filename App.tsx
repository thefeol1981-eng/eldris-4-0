import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import type {CSSProperties,FormEvent,ReactNode} from 'react';
import {Navigate,NavLink,Route,Routes,useLocation} from 'react-router-dom';
import {
  Activity,AlertTriangle,Bell,Bot,Camera,Check,CheckCircle2,CircleHelp,Clock3,Command,
  ConciergeBell,Cpu,Droplets,Eye,Gauge,HeartHandshake,HeartPulse,Home,LockKeyhole,MapPin,
  Menu,MessageSquare,Mic,Plus,Radio,RefreshCw,Search,Send,Settings as SettingsIcon,ShieldCheck,
  Siren,Sparkles,Square,Thermometer,Trash2,TriangleAlert,Video,VideoOff,Waves,Volume2,Wifi,WifiOff,X,Zap,
  ArrowUp,ArrowDown,ArrowLeft,ArrowRight,
} from 'lucide-react';
import {io,Socket} from 'socket.io-client';
import {request,apiBase} from './api';

/* ---------------------------------------------------------------- types */
type Status={drive_connected:boolean;robot_connected:boolean;drive_url:string|null;drive_hint:string|null;drive_error:string|null;camera_available:boolean;mode:string;movement:string;speed_pct:number|null;rssi:number|null;last_command:string|null;face_present:boolean|null;last_detection:string|null;updated_at:string};
type Reminder={id:number;title:string;category:string;scheduled_at:string;enabled:boolean;status:string};
type AlertItem={id:number;type:string;severity:string;description:string;status:string;created_at:string;simulated:boolean};
type EventItem={id:number;category:string;message:string;created_at:string};
type Caregiver={id:number;full_name:string;email:string;created_at?:string|null};
type Vars=CSSProperties & Record<string,string|number>;
/** One home climate sensor. There is deliberately no per-room breakdown. */
type HomeClimate={temperature_c:number|null;humidity_pct:number|null;comfort:string;humidity_state:string;online:boolean;source:string|null;reported:boolean;battery_pct:number|null;updated_at:string|null};
type ClimatePayload={home:HomeClimate;summary:{temperature_c:number|null;humidity_pct:number|null;comfort:string;humidity_state:string};updated_at:string};
type NavigationRoom={room:string;room_name:string;floor:number};
type Device={id:string;kind:string;name:string;serial?:string|null;address?:string|null;room:string|null;room_name:string|null;note:string;battery_pct:number|null;online:boolean;low_battery?:boolean;last_seen_at?:string|null;follows_robot?:boolean;error?:string|null;hand?:string|null};
type DevicesPayload={items:Device[];summary:{total:number;online:number;offline:number;by_kind:Record<string,number>};updated_at:string};
type RobotLocation={room:string;room_name:string;camera:string;cameras?:string[];camera_online:boolean;updated_at:string};
type ChatTurn={role:'user'|'assistant';content:string;source?:string};
/** Heart rate, blood oxygen and body temperature from the single wrist band. */
type Vitals={body_temperature_c:number|null;heart_rate_bpm:number|null;spo2_pct:number|null;stale:boolean;updated_at:string|null};
/** The one band, worn on the left wrist, as returned inside GET /api/motion. */
type Wearable={id:string;name:string;hand:string;configured_hand?:string;note:string;battery_pct:number|null;online:boolean;sample_rate_hz:number;last_sample_at:string|null;posture:string;activity_g:number;magnitude_g:number;axes:{x:number|null;y:number|null;z:number|null};tremor:{active:boolean;frequency_hz:number|null;amplitude_g:number|null;band:number[];since:string|null};fall:{suspected:boolean;last_at:string|null};vitals:Vitals;trace:number[]};
type MotionEventItem={id:number;kind:string;level:string;device:string;device_name:string;hand:string;detail:string;value:Record<string,unknown>;source:string;created_at:string;simulated?:boolean};
type MotionPayload={wearables:Wearable[];events:MotionEventItem[];summary:{wearables_total:number;wearables_online:number;tremor_active:number;tremor_devices:string[];falls_today:number;last_fall_at:string|null;activity_g:number;tremor_band_hz:number[];heart_rate_bpm:number|null;spo2_pct:number|null;body_temperature_c:number|null;vitals_updated_at:string|null};settings:{tremor_band_hz:number[];tremor_amplitude_g:number;tremor_sustain_s:number;fall_impact_g:number;fall_tilt_deg:number;fall_confirm_s:number;sample_rate_hz:number};updated_at:string;emergency:Emergency};
/** The whole-app verdict: while active the dashboard paints itself red. */
type Emergency={active:boolean;level:string;headline:string;detail:string;since:string|null;sources:{kind:string;id?:number|string;type:string;severity:string;detail:string;at:string|null}[];checked_at:string};
type CameraSource={id:string;name:string;room_name:string|null;follows_robot:boolean;stream_configured:boolean;snapshot_configured:boolean;ip:string|null;online:boolean;checked_at:number|null;error:string|null};
type CameraPayload={available:boolean;stream_configured:boolean;live:number;sources:CameraSource[];face_present:boolean|null;last_detection:string|null;frames_stored:boolean;note:string};
/** The chatbot ESP32 (mic, amplifier, speaker) and the five help phrases. */
type HelpPayload={device:{id:string;name:string;online:boolean;last_seen_at:string|null;note:string};keywords:string[];last_trigger:{keyword:string|null;heard:string;at:string|null;device:string}|null;cooldown_seconds_left:number;recording:{active:boolean;seconds_left:number;window_seconds:number;until:string|null;stores_audio_on_server:boolean;note:string};approach:{room:string;approach_seconds:number;enabled:boolean;note:string};patient_room:string;updated_at:string};

const KIND_LABEL:Record<string,string>={hub:'Hub',controller:'Drive ESP32',robot:'Robot',camera:'ESP32-CAM',audio:'Audio',sensor:'Home ESP32',wearable:'Wrist band ESP32'};

const nav=[
  ['Dashboard','/',Home],
  ['Body & Motion','/motion',HeartPulse],
  ['Robot Control','/control',Command],
  ['Cameras','/camera',Camera],
  ['Assistant','/assistant',MessageSquare],
  ['Reminders','/reminders',Bell],
  ['Caregiver Alerts','/alerts',AlertTriangle],
  ['Devices','/devices',Cpu],
  ['Activity History','/activity',Activity],
  ['Settings','/settings',SettingsIcon],
] as const;
const stamp=(s?:string|null)=>s?new Date(s).toLocaleString():'Not recorded';
/** Presentation helper: shifts the greeting with the caregiver's local clock. */
const vars=(i:number):Vars=>({'--i':i});
const greeting=(name:string)=>{const h=new Date().getHours();const word=h<12?'Good morning':h<17?'Good afternoon':'Good evening';return `${word}, ${name.split(' ')[0]}`;};

/** Ticking wall clock — the caregiver asked for the time on screen. */
function useClock(){
  const [now,setNow]=useState(()=>new Date());
  useEffect(()=>{const t=setInterval(()=>setNow(new Date()),1000);return()=>clearInterval(t);},[]);
  return now;
}
function LiveClock(){
  const now=useClock();
  return <div className="date-chip" title="Caregiver local time"><Clock3 size={15}/><b>{now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}</b><span>{now.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'})}</span></div>;
}

/** Ambient aurora + grain layer that the frosted surfaces refract. Decorative only. */
function Ambient(){return <div className="ambient" aria-hidden="true"><span className="orb orb-1"/><span className="orb orb-2"/><span className="orb orb-3"/><span className="orb orb-4"/><div className="grain"/></div>;}

function BrandBlock({compact=false}:{compact?:boolean}){return <div className="brand"><div className="brand-mark"><Bot size={25}/><HeartPulse size={13} className="brand-heart"/></div><div><strong>ELDRIS <span>3.0</span></strong><small>{compact?'Care companion':'Care companion · Concierge'}</small></div></div>;}

/* ------------------------------------------------------------------ app */
function App(){
  const [user,setUser]=useState<Caregiver|null>(null);
  const [guest,setGuest]=useState(true);
  const [checking,setChecking]=useState(true);
  const [view,setView]=useState<'app'|'auth'>('app');

  /* The dashboard opens by itself: a local guest session is requested when no
     caregiver is signed in. The sign-in page still exists, but it never blocks
     the person watching the home. */
  useEffect(()=>{let active=true;(async()=>{
    try{
      const me=await request<{authenticated:boolean;user:Caregiver|null;guest?:boolean}>('/api/auth/me');
      if(me.authenticated&&me.user){if(active){setUser(me.user);setGuest(!!me.guest);}return;}
      const g=await request<{user:Caregiver;guest:boolean}>('/api/auth/guest',{method:'POST'});
      if(active){setUser(g.user);setGuest(true);}
    }catch{
      if(active){setUser({id:0,full_name:'Guest caregiver',email:'Local guest session'});setGuest(true);}
    }finally{if(active)setChecking(false);}
  })();return()=>{active=false;};},[]);

  async function signOut(){
    try{await request('/api/auth/logout',{method:'POST'});}catch{}
    try{const g=await request<{user:Caregiver}>('/api/auth/guest',{method:'POST'});setUser(g.user);setGuest(true);setView('app');}
    catch{setUser({id:0,full_name:'Guest caregiver',email:'Local guest session'});setGuest(true);setView('app');}
  }

  if(checking)return <><Ambient/><div className="auth-page"><div style={{gridColumn:'1/-1',display:'grid',placeItems:'center'}}><div className="auth-loading">Opening the ELDRIS dashboard…</div></div></div></>;
  if(view==='auth'||!user)return <><Ambient/><AuthScreen onSignedIn={u=>{setUser(u);setGuest(false);setView('app');}} onBack={user?()=>setView('app'):undefined}/></>;
  return <><Ambient/><DashboardApp user={user} guest={guest} onSignIn={()=>setView('auth')} onLogout={signOut}/></>;
}

function AuthScreen({onSignedIn,onBack}:{onSignedIn:(u:Caregiver)=>void;onBack?:()=>void}){
  const [mode,setMode]=useState<'signup'|'signin'>('signin');
  const [fullName,setFullName]=useState('');const [email,setEmail]=useState('');const [password,setPassword]=useState('');const [confirm,setConfirm]=useState('');const [showPassword,setShowPassword]=useState(false);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [notice,setNotice]=useState('');
  async function submit(e:FormEvent){e.preventDefault();setError('');setNotice('');if(mode==='signup'&&password!==confirm){setError('Passwords do not match.');return;}setBusy(true);try{if(mode==='signup'){await request('/api/auth/signup',{method:'POST',body:JSON.stringify({full_name:fullName,email:email.trim(),password})});setMode('signin');setPassword('');setConfirm('');setNotice('Account created. Sign in with your new account.');}else{const r=await request<{user:Caregiver}>('/api/auth/signin',{method:'POST',body:JSON.stringify({email:email.trim(),password})});onSignedIn(r.user);}}catch(err){setError(err instanceof Error?err.message:'Unable to complete request.');}finally{setBusy(false);}}
  return <main className="auth-page">
    <section className="auth-hero">
      <BrandBlock compact/>
      <div className="auth-kicker">PRIVATE CAREGIVER LOUNGE</div>
      <h2>Care that feels<br/><em>effortlessly attended.</em></h2>
      <p>ELDRIS 3.0 is a discreet care companion for the people you look after — quiet monitoring, gentle reminders and a caregiver who is always one glance away.</p>
      <ul className="auth-pillars">
        <li><ConciergeBell size={17}/><span><b>Always attended</b>Live body vitals, home climate and reminders in a single calm view.</span></li>
        <li><HeartHandshake size={17}/><span><b>Built for care, not surveillance</b>Face presence only. No identity recognition, no stored frames.</span></li>
        <li><LockKeyhole size={17}/><span><b>Private by default</b>Hashed passwords and an encrypted session keep this lounge yours alone.</span></li>
      </ul>
    </section>
    <section className="auth-card">
      <BrandBlock/>
      <div className="auth-intro">
        <div className="auth-kicker">OPTIONAL CAREGIVER ACCOUNT</div>
        <h1>{mode==='signup'?'Create your caregiver account':'Welcome back'}</h1>
        <p>{mode==='signup'?'A named account keeps your reminders and alerts separate from the guest view.':'Sign in to use your named caregiver session instead of the guest view.'}</p>
      </div>
      <form className="auth-form" onSubmit={submit}>
        {mode==='signup'&&<label>Full name<input autoComplete="name" value={fullName} onChange={e=>setFullName(e.target.value)} placeholder="Your full name" minLength={2} maxLength={80} required/></label>}
        <label>Email address<input type="email" autoComplete="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@example.com" maxLength={254} required/></label>
        <label>Password<div className="password-wrap"><input type={showPassword?'text':'password'} autoComplete={mode==='signup'?'new-password':'current-password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder={mode==='signup'?'At least 10 characters':'Enter your password'} minLength={mode==='signup'?10:1} maxLength={128} required/><button type="button" className="password-toggle" onClick={()=>setShowPassword(!showPassword)} aria-label={showPassword?'Hide password':'Show password'}><Eye size={16}/>{showPassword?'Hide':'Show'}</button></div></label>
        {mode==='signup'&&<label>Confirm password<input type={showPassword?'text':'password'} autoComplete="new-password" value={confirm} onChange={e=>setConfirm(e.target.value)} placeholder="Re-enter your password" minLength={10} maxLength={128} required/></label>}
        {error&&<div className="auth-error" role="alert">{error}</div>}
        {notice&&<div className="auth-success" role="status">{notice}</div>}
        <button className="auth-submit" type="submit" disabled={busy}>{busy?(mode==='signup'?'Creating account…':'Signing in…'):(mode==='signup'?'Create account':'Sign in')}</button>
      </form>
      <div className="auth-switch">{mode==='signup'?'Already have an account?':'New to ELDRIS?'} <button onClick={()=>{setError('');setNotice('');setMode(mode==='signup'?'signin':'signup');}}>{mode==='signup'?'Sign in':'Create account'}</button></div>
      {onBack&&<button className="auth-back" onClick={onBack}><ArrowLeft size={15}/> Back to the dashboard</button>}
      <div className="auth-security"><ShieldCheck size={16}/><span>Your account is stored locally. Passwords are hashed on the server and are never saved as plain text.</span></div>
    </section>
    <div className="auth-footer">ELDRIS 3.0 · Smart Assistance. Safer Everyday Care.<br/>Prototype only · Not an emergency-response service</div>
  </main>;
}

/* --------------------------------------------------------------- shell */
function DashboardApp({user,guest,onSignIn,onLogout}:{user:Caregiver;guest:boolean;onSignIn:()=>void;onLogout:()=>void}){
  const [status,setStatus]=useState<Status|null>(null),[reminders,setReminders]=useState<Reminder[]>([]),[alerts,setAlerts]=useState<AlertItem[]>([]),[events,setEvents]=useState<EventItem[]>([]),[error,setError]=useState(''),[mobile,setMobile]=useState(false),[refreshing,setRefreshing]=useState(false);
  const [climate,setClimate]=useState<ClimatePayload|null>(null),[devices,setDevices]=useState<DevicesPayload|null>(null),[robot,setRobot]=useState<RobotLocation|null>(null),[rooms,setRooms]=useState<NavigationRoom[]>([]);
  const [motion,setMotion]=useState<MotionPayload|null>(null),[emergency,setEmergency]=useState<Emergency|null>(null),[camera,setCamera]=useState<CameraPayload|null>(null),[help,setHelp]=useState<HelpPayload|null>(null);
  const location=useLocation();
  const refresh=useCallback(async()=>{setRefreshing(true);try{const [s,r,a,e]=await Promise.all([request<Status>('/api/status'),request<{items:Reminder[]}>('/api/reminders'),request<{items:AlertItem[]}>('/api/alerts'),request<{items:EventItem[]}>('/api/activity')]);setStatus(s);setReminders(r.items);setAlerts(a.items);setEvents(e.items);setError('');}catch(e){setError(e instanceof Error?e.message:'Backend unavailable');setStatus(null);}finally{setRefreshing(false);}
    // Climate, devices, robot location and navigation zones are fetched on their
    // own so an older backend leaves the rest of the dashboard working.
    try{const [cl,dv,rl,zs]=await Promise.all([request<ClimatePayload>('/api/readings'),request<DevicesPayload>('/api/devices'),request<RobotLocation>('/api/robot/location'),request<{items:NavigationRoom[]}>('/api/robot/rooms')]);setClimate(cl);setDevices(dv);setRobot(rl);setRooms(zs.items||[]);}catch{}
    // The wrist band, the emergency verdict and the cameras likewise.
    try{const mo=await request<MotionPayload>('/api/motion');setMotion(mo);setEmergency(mo.emergency);}catch{}
    try{setCamera(await request<CameraPayload>('/api/camera'));}catch{}
    // The chatbot ESP32 and its five help phrases.
    try{setHelp(await request<HelpPayload>('/api/voice/help'));}catch{}}
  ,[]);
  useEffect(()=>{refresh();const timer=setInterval(refresh,5000);let socket:Socket|undefined;try{socket=io(apiBase||window.location.origin,{path:'/socket.io',transports:['websocket','polling']});socket.on('status_update',refresh);socket.on('activity_update',refresh);socket.on('reading_update',refresh);socket.on('motion_update',refresh);socket.on('voice_update',refresh);socket.on('emergency_update',(state:Emergency)=>setEmergency(state));}catch{}return()=>{clearInterval(timer);socket?.disconnect();};},[refresh]);
  /* The red emergency palette is a data attribute on <html>, so one piece of
     state repaints every surface without touching any component tree. */
  const emergencyActive=!!emergency?.active;
  useEffect(()=>{const root=document.documentElement;if(emergencyActive)root.setAttribute('data-emergency','1');else root.removeAttribute('data-emergency');return()=>root.removeAttribute('data-emergency');},[emergencyActive]);
  const acknowledge=useCallback(async()=>{try{const r=await request<{emergency:Emergency}>('/api/emergency/acknowledge',{method:'POST'});setEmergency(r.emergency);}catch{}await refresh();},[refresh]);
  const pending=alerts.filter(a=>a.status==='open'||a.status==='acknowledged').length;
  const title=nav.find(n=>n[1]===location.pathname)?.[0]||'Dashboard';
  return <div className="app-shell">
    <aside className={`sidebar ${mobile?'show':''}`}>
      <BrandBlock compact/>
      <div className="profile"><div className="avatar">{(user.full_name||'GC').slice(0,2).toUpperCase()}</div><div><b>{user.full_name}</b><small>{guest?'Guest session · no account needed':user.email}</small></div>{guest&&<button className="profile-signin" onClick={onSignIn}>Sign in</button>}<button className="mobile-close" onClick={()=>setMobile(false)} aria-label="Close navigation"><X size={18}/></button></div>
      <nav>{nav.map(([label,path,Icon])=><NavLink key={path} to={path} end={path==='/'} onClick={()=>setMobile(false)} className={({isActive})=>`nav-link ${isActive?'active':''}`}><Icon size={18}/><span>{label}</span>{label==='Caregiver Alerts'&&pending>0&&<em>{pending}</em>}</NavLink>)}</nav>
      <div className="sidebar-bottom"><ShieldCheck size={17}/><div><b>Privacy-first prototype</b><small>One wrist band · Direct ESP32 control · No cloud video</small></div></div>
    </aside>
    {mobile&&<div className="scrim" onClick={()=>setMobile(false)}/>}
    <main className="main">
      <header className="topbar">
        <button className="menu-toggle" onClick={()=>setMobile(true)} aria-label="Open navigation"><Menu size={19}/></button>
        <div><div className="eyebrow">ELDRIS CARE SYSTEM</div><h1>{title}</h1></div>
        <div className="top-actions">
          <LiveClock/>
          {emergencyActive&&<span className="emergency-chip"><span className="dot"/>{emergency?.headline||'Emergency'}</span>}
          <span className={`connection-pill ${status?.drive_connected?'good':'bad'}`}><span className="dot"/>{status?.drive_connected?'Drive ESP32 online':'Drive ESP32 offline'}</span>
          <button className="icon-button" title="Refresh data" aria-label="Refresh data" onClick={refresh}><RefreshCw size={17} className={refreshing?'spin':''}/></button>
          {guest?<button className="signout-button" onClick={onSignIn}>Sign in</button>:<button className="signout-button" onClick={onLogout}>Sign out</button>}
        </div>
      </header>
      <div className="content">
        {emergency&&<EmergencyBanner emergency={emergency} onAcknowledge={acknowledge}/>}
        {error&&<div className="error-banner"><WifiOff size={20}/><div><b>Backend unavailable</b><span>{error}. Start the Flask API to load live status. Hardware-dependent controls remain disabled.</span></div><button onClick={refresh}>Retry</button></div>}
        <div className="page" key={location.pathname}>
          <Routes>
            <Route path="/" element={<Dashboard user={user} status={status} reminders={reminders} alerts={alerts} events={events} climate={climate} robot={robot} motion={motion} emergency={emergency} camera={camera}/>}/>
            <Route path="/motion" element={<MotionPage motion={motion} emergency={emergency} onRefresh={refresh} onAcknowledge={acknowledge}/>}/>
            <Route path="/control" element={<Control status={status} refresh={refresh} robot={robot} camera={camera} rooms={rooms} onRelocate={refresh}/>}/>
            <Route path="/camera" element={<CameraPage status={status} robot={robot} camera={camera} onRefresh={refresh}/>}/>
            <Route path="/assistant" element={<Assistant robot={robot} climate={climate} motion={motion} help={help}/>}/>
            <Route path="/reminders" element={<Reminders reminders={reminders} refresh={refresh}/>}/>
            <Route path="/alerts" element={<Alerts alerts={alerts} refresh={refresh}/>}/>
            <Route path="/devices" element={<DevicesPage devices={devices} robot={robot}/>}/>
            <Route path="/activity" element={<History events={events}/>}/>
            <Route path="/settings" element={<Settings status={status} refresh={refresh} devices={devices} climate={climate} guest={guest} onSignIn={onSignIn} onSignOut={onLogout}/>}/>
            <Route path="*" element={<Navigate to="/" replace/>}/>
          </Routes>
        </div>
        <footer>ELDRIS 3.0 <span>·</span> Smart Assistance. Safer Everyday Care. <span className="footer-note">Prototype only · Not an emergency-response service</span></footer>
      </div>
    </main>
  </div>;
}

/** The alarm banner. The red palette itself comes from the data-emergency attribute. */
function EmergencyBanner({emergency,onAcknowledge}:{emergency:Emergency;onAcknowledge:()=>void}){
  const [busy,setBusy]=useState(false);
  if(!emergency.active)return null;
  const critical=emergency.level==='critical';
  async function acknowledge(){setBusy(true);try{await onAcknowledge();}finally{setBusy(false);}}
  return <>
    <div className="alarm-vignette" aria-hidden="true"/>
    <div className={`emergency-banner ${critical?'critical':''}`} role="alert" aria-live="assertive">
      <div className="emergency-icon">{critical?<Siren size={24}/>:<TriangleAlert size={24}/>}</div>
      <div className="emergency-copy">
        <b>{emergency.headline}</b>
        <p>{emergency.detail}</p>
        <small>{emergency.sources.length} active source{emergency.sources.length===1?'':'s'}{emergency.since?` · raised ${stamp(emergency.since)}`:''}</small>
      </div>
      <div className="emergency-actions">
        <button className="emergency-ack" onClick={acknowledge} disabled={busy}>{busy?'Acknowledging…':'Acknowledge'}</button>
      </div>
    </div>
  </>;
}

/* ------------------------------------------------------------ dashboard */
function SectionHead({title,sub,action}:{title:string;sub?:string;action?:ReactNode}){return <div className="section-head"><div><h2>{title}</h2>{sub&&<p>{sub}</p>}</div>{action}</div>;}
function Stat({label,value,detail,icon:Icon,tone='neutral',delay=0}:{label:string;value:string;detail:string;icon:any;tone?:string;delay?:number}){return <div className="stat-card" style={vars(delay)}><div className={`stat-icon ${tone}`}><Icon size={20}/></div><div className="stat-copy"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></div>;}
function StatusTag({good,children}:{good:boolean;children:ReactNode}){return <span className={`status-tag ${good?'success':'danger'}`}><span className="dot"/>{children}</span>;}
function pendingTone(a:AlertItem[]){return a.some(x=>x.status!=='resolved')?'red':'green';}

/** Ask the backend to relocate the mobile robot. Returns an error string, or null on success. */
async function relocate(room:string):Promise<string|null>{
  try{await request('/api/robot/location',{method:'POST',body:JSON.stringify({room})});return null;}
  catch(e){return e instanceof Error?e.message:'Could not move the robot';}
}

function num(value:number|null|undefined,digits=1,suffix=''){return value==null?'—':`${value.toFixed(digits)}${suffix}`;}

/** Colour bands for vitals. Advisory only; this app is not a medical device. */
function heartTone(v:number|null){if(v==null)return 'neutral';if(v<50||v>120)return 'bad';if(v<60||v>100)return 'warn';return 'good';}
function spo2Tone(v:number|null){if(v==null)return 'neutral';if(v<90)return 'bad';if(v<95)return 'warn';return 'good';}
function tempTone(v:number|null){if(v==null)return 'neutral';if(v<35.5||v>38)return 'bad';if(v<36||v>37.5)return 'warn';return 'good';}
function ratio(value:number|null,min:number,max:number){if(value==null)return 0;return Math.max(.06,Math.min(1,(value-min)/(max-min)));}

/** One animated ring gauge. The arc length animates with the CSS transition. */
function VitalRing({label,value,unit,digits,pct,tone,icon:Icon,note}:{label:string;value:number|null;unit:string;digits:number;pct:number;tone:string;icon:any;note:string}){
  const dash=2*Math.PI*52;
  return <div className={`vital-ring ${tone}`} style={{'--dash':dash,'--pct':pct} as Vars}>
    <svg viewBox="0 0 120 120" className="ring-svg" aria-hidden="true">
      <circle className="ring-track" cx="60" cy="60" r="52"/>
      <circle className="ring-value" cx="60" cy="60" r="52" style={{strokeDashoffset:dash*(1-Math.max(0,Math.min(1,pct)))}}/>
    </svg>
    <div className="vital-ring-copy">
      <span className="vital-icon"><Icon size={14}/></span>
      <strong>{value==null?'—':value.toFixed(digits)}<em>{unit}</em></strong>
      <small>{label}</small>
      <span className="vital-ring-note">{note}</span>
    </div>
  </div>;
}

/** The body vitals from the single wrist band. */
function VitalsPanel({wearable,delay=0}:{wearable:Wearable|null;delay?:number}){
  const v=wearable?.vitals;
  const live=!!wearable&&wearable.online;
  const hr=v?.heart_rate_bpm??null, spo2=v?.spo2_pct??null, body=v?.body_temperature_c??null;
  return <section className="panel vitals-panel" style={vars(delay)}>
    <div className="vitals-head">
      <div className="vitals-head-copy">
        <span className="kicker"><Sparkles size={12}/> BODY VITALS</span>
        <h2>{wearable?.name||'Wrist band'}</h2>
        <p>{wearable?`Worn on the ${wearable.hand} wrist · heart rate, oxygen, body temperature, tremor and falls all come from this one band`:'Waiting for the wrist band to report.'}</p>
      </div>
      <div className="vitals-live">
        <span className={`pulse-dot ${live?'live':''}`}/>
        <div><b>{live?'Live':wearable?'Band offline':'No report'}</b><small>{!wearable?'Waiting for the band':v?.stale?'Last packet is old':'Updated just now'}</small></div>
      </div>
    </div>
    <div className="vital-rings">
      <VitalRing label="Heart rate" value={hr} unit="bpm" digits={0} pct={ratio(hr,40,140)} tone={heartTone(hr)} icon={HeartPulse} note={hr==null?'No reading':hr>=60&&hr<=100?'Resting range':'Outside resting range'}/>
      <VitalRing label="Blood oxygen" value={spo2} unit="%" digits={1} pct={ratio(spo2,85,100)} tone={spo2Tone(spo2)} icon={Gauge} note={spo2==null?'No reading':spo2>=95?'Healthy range':'Below the usual range'}/>
      <VitalRing label="Body temperature" value={body} unit="°C" digits={1} pct={ratio(body,34,40)} tone={tempTone(body)} icon={Thermometer} note={body==null?'No reading':body>=36&&body<=37.5?'Within the usual range':'Check the reading'}/>
    </div>
    <div className="vitals-foot">
      <span className={`motion-badge ${wearable?.online?'live':''}`}>{wearable?.online?'Band connected':'Band offline'}</span>
      <span className={`motion-badge ${wearable?.tremor.active?'alert':''}`}>{wearable?.tremor.active?<><Zap size={11}/> Tremor detected</>:'No tremor'}</span>
      <span className="vitals-disclaimer">Advisory only · not a medical device</span>
    </div>
  </section>;
}

/** Home temperature and humidity from the one home sensor. */
function ClimatePanel({climate,delay=0}:{climate:ClimatePayload|null;delay?:number}){
  const home=climate?.home;
  const temp=home?.temperature_c??null;
  const humidity=home?.humidity_pct??null;
  const comfort=home?.comfort||'unknown';
  const comfortPct=temp==null?0:Math.max(4,Math.min(100,((temp-14)/(30-14))*100));
  return <section className="panel climate-panel" style={vars(delay)}>
    <SectionHead title="Home climate" sub="One ESP32 for the whole home. No room-by-room tracking." action={<span className={`status-tag ${home?.online?'success':'danger'}`}><span className="dot"/>{home?.online?'Online':home?.reported?'Stale':'Never reported'}</span>}/>
    <div className="climate-hero">
      <div className="climate-temp">
        <Thermometer size={20}/>
        <strong>{num(temp,1)}</strong>
        <span>°C</span>
      </div>
      <div className="climate-humidity">
        <Droplets size={18}/>
        <div><b>{num(humidity,0,'%')}</b><small>Humidity · {home?.humidity_state||'unknown'}</small></div>
      </div>
    </div>
    <div className={`comfort-meter ${comfort.replace(' ','-')}`}>
      <span className="comfort-fill" style={{width:`${comfortPct}%`}}/>
    </div>
    <div className="climate-labels"><span>Cool</span><b>{comfort}</b><span>Warm</span></div>
    <div className="climate-foot">
      <span className={`motion-badge ${home?.online?'live':''}`}>{home?.reported?'Live hardware':'Waiting for ESP32 #3'}</span>
      <small>{home?.updated_at?`Updated ${stamp(home.updated_at)}`:'Set ESP32_HOME_IP and power the DHT22 node'}</small>
    </div>
  </section>;
}

/** One wheeled robot, two body cameras, and a control to move it. */
function CameraLive({source,compact=false}:{source:CameraSource|null;compact?:boolean}){
  const [failed,setFailed]=useState(false);
  const [nonce,setNonce]=useState(0);
  useEffect(()=>{setFailed(false);},[source?.id,nonce]);
  const cls=`camera-live ${compact?'compact':''}`;
  if(!source)return <div className={cls}><div className="camera-empty"><Camera size={30}/><b>No camera registered</b><span>The robot has not reported a body camera yet.</span></div></div>;
  if(!source.stream_configured)return <div className={cls}><div className="camera-empty"><Video size={30}/><b>{source.name} has no address yet</b><span>Put this ESP32-CAM's IP in backend/.env (CAMERA_ESP_FRONT_IP or CAMERA_ESP_REAR_IP) and restart the API. The picture appears here automatically.</span></div></div>;
  return <div className={cls}>
    <span className="live-tag"><i/>Live · {source.name}</span>
    {failed
      ?<div className="camera-empty"><VideoOff size={28}/><b>Stream unavailable</b><span>The server could not reach this ESP32-CAM{source.ip?` at ${source.ip}`:''}. Check its power and 2.4 GHz Wi-Fi.</span><button className="secondary-button small" onClick={()=>setNonce(n=>n+1)}>Try again</button></div>
      :<img src={`${apiBase}/api/camera/${source.id}/stream?t=${nonce}`} alt={`${source.name} live view`} onError={()=>setFailed(true)}/>}
  </div>;
}

function CameraTabs({sources,active,onPick}:{sources:CameraSource[];active:CameraSource|null;onPick:(id:string)=>void}){
  if(!sources.length)return null;
  return <div className="segmented" role="tablist" aria-label="Choose a body camera">
    {sources.map(source=><button key={source.id} role="tab" aria-selected={active?.id===source.id} className={active?.id===source.id?'active':''} onClick={()=>onPick(source.id)}><Camera size={14}/>{source.name}</button>)}
  </div>;
}

function Dashboard({user,status:s,reminders,alerts,events,climate,robot,motion,emergency,camera}:{user:Caregiver;status:Status|null;reminders:Reminder[];alerts:AlertItem[];events:EventItem[];climate:ClimatePayload|null;robot:RobotLocation|null;motion:MotionPayload|null;emergency:Emergency|null;camera:CameraPayload|null}){
  const upcoming=reminders.filter(r=>r.enabled&&new Date(r.scheduled_at)>=new Date()).slice(0,4);
  const handSummary=motion?.summary;
  const band=motion?.wearables[0]||null;
  const camerasLive=camera?.sources.filter(c=>c.online).length||0;
  return <>
    <div className="welcome-row">
      <div><h2>{greeting(user.full_name)}</h2><p>Your care companion at a glance: body vitals, home climate and the robot.</p></div>
      <LiveClock/>
    </div>
    {!s?.drive_connected&&<div className="offline-card"><div className="offline-icon"><WifiOff/></div><div><b>Drive ESP32 is not answering</b><p>{s?.drive_hint||`The server could not reach the drive board${s?.drive_url?` at ${s.drive_url}`:''}. Check its power, Wi-Fi and DRIVE_ESP_IP.`}</p></div><span className="offline-label">OFFLINE</span></div>}
    <div className="vitals-layout">
      <VitalsPanel wearable={band} delay={0}/>
      <ClimatePanel climate={climate} delay={1}/>
    </div>
    <div className="stats-grid">
      <Stat delay={0} label="Drive ESP32" value={s?.drive_connected?'Online':'Offline'} detail={s?.drive_url||'No DRIVE_ESP_IP set'} icon={s?.drive_connected?Wifi:WifiOff} tone={s?.drive_connected?'green':'red'}/>
      <Stat delay={1} label="ESP32-CAM" value={`${camerasLive} of 2`} detail={`${camera?.sources[0]?.name||'Front'} · ${camera?.sources[1]?.name||'Rear'}`} icon={Camera} tone={camerasLive===2?'green':'amber'}/>
      <Stat delay={2} label="Today's reminders" value={String(reminders.filter(r=>r.enabled&&new Date(r.scheduled_at).toDateString()===new Date().toDateString()).length)} detail={`${reminders.filter(r=>r.enabled).length} enabled total`} icon={Bell} tone="amber"/>
      <Stat delay={3} label="Pending alerts" value={String(alerts.filter(a=>a.status!=='resolved').length)} detail="Requires caregiver review" icon={AlertTriangle} tone={pendingTone(alerts)}/>
    </div>
    {!band?.online&&<div className="info-note"><Waves size={18}/><span>ESP32 #1 (wrist band) is not reporting. Power the band and check that <code>DEVICE_API_KEY</code> matches so it can post to <code>/api/motion</code>.</span></div>}
    <div className="dashboard-grid">
      <section className="panel"><SectionHead title="Robot status" sub="Connection and subsystem health"/>
        <div className="health-list">
          <Health label="Drive ESP32 (#2)" value={s?.drive_connected?'Connected':'Disconnected'} good={!!s?.drive_connected}/>
          <Health label="Wrist band ESP32 (#1)" value={band?.online?'Connected':'No report'} good={!!band?.online}/>
          <Health label="Home climate ESP32 (#3)" value={climate?.home.online?'Connected':climate?.home.reported?'Stale':'No report'} good={!!climate?.home.online}/>
          <Health label="ESP32-CAM body cameras" value={`${camerasLive} of 2 online`} good={camerasLive===2}/>
          <Health label="Current location" value={robot?.room_name||'Unknown'} good={!!robot}/>
          <Health label="Operating mode" value={s?.mode||'Unknown'} good={!!s?.drive_connected}/>
        </div>
        <div className="panel-foot">Last update: {stamp(s?.updated_at)}</div>
      </section>
      <section className="panel"><SectionHead title="Upcoming reminders" sub="Scheduled by the caregiver"/>
        {upcoming.length?upcoming.map(r=><div className="reminder-row" key={r.id}><div className="reminder-icon"><Bell size={17}/></div><div className="grow"><b>{r.title}</b><small>{r.category}</small></div><time>{new Date(r.scheduled_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}<small>{new Date(r.scheduled_at).toLocaleDateString([],{month:'short',day:'numeric'})}</small></time></div>):<Empty icon={Bell} title="No upcoming reminders" sub="Add a reminder to create a caregiver schedule."/>}
      </section>
      <section className={`panel ${emergency?.active?'emergency-panel':''}`}>
        <SectionHead title="Wrist band & falls" sub="One band, on the left wrist" action={<NavLink className="secondary-button" to="/motion">Open</NavLink>}/>
        {band?<>
          <div className="health-row"><span>Heart rate</span><div><b>{num(band.vitals.heart_rate_bpm,0,' bpm')}</b><span className={`mini-dot ${heartTone(band.vitals.heart_rate_bpm)==='good'?'green':'grey'}`}/></div></div>
          <div className="health-row"><span>Blood oxygen</span><div><b>{num(band.vitals.spo2_pct,1,'%')}</b><span className={`mini-dot ${spo2Tone(band.vitals.spo2_pct)==='good'?'green':'grey'}`}/></div></div>
          <div className="health-row"><span>Body temperature</span><div><b>{num(band.vitals.body_temperature_c,1,'°C')}</b></div></div>
          <div className="health-row"><span>Tremor</span><div>{band.tremor.active?<span className="danger-text">Active · {num(band.tremor.frequency_hz,1,' Hz')}</span>:<span>None · steady hand</span>}</div></div>
        </>:<Empty icon={Waves} title="No wrist band reporting" sub="The band posts vitals and movement to /api/motion once it is powered."/>}
        <div className="panel-foot">{handSummary?.last_fall_at?`Most recent fall: ${stamp(handSummary.last_fall_at)}`:'No fall recorded since the band started.'}</div>
      </section>
      <section className="panel"><SectionHead title="Recent activity" sub="Latest system events"/><Timeline events={events.slice(0,5)}/></section>
      <section className="panel"><SectionHead title="Caregiver attention" sub="Open alerts and warnings"/>
        {alerts.filter(a=>a.status!=='resolved').slice(0,4).map(a=><div className="alert-row" key={a.id}><AlertTriangle size={18} className={a.severity==='critical'?'danger-text':'warning-text'}/><div className="grow"><b>{a.type.replaceAll('_',' ')}</b><small>{a.description}</small><small>{stamp(a.created_at)}</small></div><span className={`severity ${a.severity}`}>{a.severity}</span></div>)}
        {!alerts.some(a=>a.status!=='resolved')&&<Empty icon={CheckCircle2} title="No pending alerts" sub="New alerts will appear here."/>}
      </section>
    </div>
  </>;
}

/* -------------------------------------------------------------- control */
function Control({status:s,refresh,robot,camera,rooms,onRelocate}:{status:Status|null;refresh:()=>void;robot:RobotLocation|null;camera:CameraPayload|null;rooms:NavigationRoom[];onRelocate?:()=>void}){
  const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[moving,setMoving]=useState(''),[moveErr,setMoveErr]=useState('');
  const [cameraId,setCameraId]=useState('');
  const sources=camera?.sources||[];
  const activeSource=sources.find(x=>x.id===cameraId)||sources[0]||null;
  const ready=!!s?.drive_connected;
  async function move(command:string){setBusy(true);setMessage('');try{const d=await request<{message:string}>('/api/movement',{method:'POST',body:JSON.stringify({command})});setMessage(d.message);refresh();}catch(e){setMessage(e instanceof Error?e.message:'Command failed');}finally{setBusy(false);}}
  async function moveTo(room:string){setMoving(room);setMoveErr('');const err=await relocate(room);setMoving('');setMoveErr(err||'');if(!err)onRelocate?.();}
  return <>
    <SectionHead title="Driving mode" sub="The robot camera sits at the top of the control area, so you can steer while you watch the path ahead."/>
    <div className="control-warning"><ShieldCheck size={20}/><div><b>Safety first</b><p>Keep the robot in a clear test area. The software stop is not a substitute for the physical emergency-stop switch.</p></div></div>
    <div className="control-layout drive-layout">
      <section className="panel drive-panel">
        <div className="drive-camera">
          <div className="drive-camera-head">
            <CameraTabs sources={sources} active={activeSource} onPick={setCameraId}/>
            <span className="drive-camera-note"><span className={`pulse-dot ${activeSource?.stream_configured?'live':''}`}/>{activeSource?activeSource.name:'No camera'}{robot?` · ${robot.room_name}`:''}</span>
          </div>
          <CameraLive source={activeSource} compact/>
        </div>
        <div className="control-top">
          <div><h3>Directional pad</h3><p>Each click sends one command. Press and hold is not supported.</p></div>
          <StatusTag good={ready}>{ready?'Control ready':'Controls disabled'}</StatusTag>
        </div>
        <div className="dpad">
          <span/>
          <button disabled={!ready||busy} onClick={()=>move('forward')} title="Move forward"><ArrowUp/></button>
          <span/>
          <button disabled={!ready||busy} onClick={()=>move('left')} title="Turn left"><ArrowLeft/></button>
          <button className="stop-button" disabled={!ready||busy} onClick={()=>move('stop')} title="Stop movement"><Square/></button>
          <button disabled={!ready||busy} onClick={()=>move('right')} title="Turn right"><ArrowRight/></button>
          <span/>
          <button disabled={!ready||busy} onClick={()=>move('reverse')} title="Move reverse"><ArrowDown/></button>
          <span/>
        </div>
        {message&&<div className="notice">{message}</div>}
        <p className="muted center">{s?.drive_connected?'Commands are sent to the drive ESP32 and confirmed by it.':'Commands require the drive ESP32 to answer its status endpoint.'}</p>
      </section>
      <section className="panel">
        <SectionHead title="Where the robot is" sub="Both body cameras travel with it"/>
        <div className="robot-room">
          <div className="robot-room-icon"><MapPin size={22}/></div>
          <div className="grow"><b>{robot?.room_name||'Unknown'}</b><small>{robot?.camera_online?'Front and rear cameras reporting together':'Cameras not reporting'} · updated {stamp(robot?.updated_at)}</small></div>
        </div>
        <div className="room-picker">
          {rooms.map(r=><button key={r.room} className={`room-chip ${robot?.room===r.room?'active':''}`} disabled={!!moving||robot?.room===r.room} onClick={()=>moveTo(r.room)}>{r.room_name}</button>)}
        </div>
        {moveErr&&<div className="form-error">{moveErr}</div>}
        <SectionHead title="Live telemetry" sub="Reported by the backend"/>
        <div className="telemetry">
          <div><span>Wi-Fi signal</span><strong>{s?.rssi==null?'Unavailable':`${s.rssi} dBm`}</strong></div>
          <div><span>Movement state</span><strong>{s?.movement||'Unknown'}</strong></div>
          <div><span>Drive ESP32</span><strong>{s?.drive_connected?'Connected':'Offline'}</strong></div>
          <div><span>Body cameras</span><strong>{camera?.sources.filter(c=>c.online).length||0} of 2 online</strong></div>
        </div>
        <p className="muted">Moving the robot relocates both ESP32-CAM boards, so the front and rear views travel with it.</p>
      </section>
    </div>
  </>;
}

/* ------------------------------------------------------------ assistant */
const QUICK_PROMPTS=['What is my heart rate?','Is my oxygen level okay?','What is my body temperature?','What is the home temperature?','Where is the robot?','Any falls today?','Are the cameras live?','What are my next reminders?','Move robot to the bedroom'];

function Assistant({robot,climate,motion,help}:{robot:RobotLocation|null;climate:ClimatePayload|null;motion:MotionPayload|null;help:HelpPayload|null}){
  const [turns,setTurns]=useState<ChatTurn[]>([{role:'assistant',content:'Hello. I can read the home temperature and humidity, report the left wrist band heart rate, oxygen and body temperature, tell you where the robot is, list reminders and alerts, and move the robot between rooms. The chatbot board also listens for five help phrases; ask me "what are the help words" to see them. What would you like to know?'}]);
  const [draft,setDraft]=useState(''),[busy,setBusy]=useState(false),[listening,setListening]=useState(false),[speak,setSpeak]=useState(false),[err,setErr]=useState('');
  const [speaking,setSpeaking]=useState(false),[speakerNote,setSpeakerNote]=useState('');
  const scrollRef=useRef<HTMLDivElement|null>(null);
  const recognitionRef=useRef<any>(null);
  const lastAssistant=useMemo(()=>[...turns].reverse().find(t=>t.role==='assistant')?.content||'',[turns]);
  const band=motion?.wearables[0]||null;

  useEffect(()=>{const el=scrollRef.current;if(el)el.scrollTop=el.scrollHeight;},[turns,busy]);
  useEffect(()=>()=>{try{recognitionRef.current?.stop?.();}catch{}try{speechSynthesis.cancel();}catch{}},[]);
  useEffect(()=>{if(!speak||!lastAssistant)return;try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(lastAssistant);u.rate=1;speechSynthesis.speak(u);}catch{}}
  ,[speak,lastAssistant]);

  async function send(text:string){
    const message=text.trim();if(!message||busy)return;
    setDraft('');setErr('');setBusy(true);
    const history=turns.map(t=>({role:t.role,content:t.content}));
    setTurns(t=>[...t,{role:'user',content:message}]);
    try{
      const r=await request<{reply:string;source:string}>('/api/chat',{method:'POST',body:JSON.stringify({message,history})});
      setTurns(t=>[...t,{role:'assistant',content:r.reply,source:r.source}]);
    }catch(e){setErr(e instanceof Error?e.message:'Assistant is unavailable');}
    finally{setBusy(false);}
  }

  /** Browser speech-to-text, only when the browser supports it. */
  function toggleMic(){
    const SR=(window as any).SpeechRecognition||(window as any).webkitSpeechRecognition;
    if(!SR){setErr('Voice input is not supported in this browser. Type your question instead.');return;}
    if(listening){try{recognitionRef.current?.stop();}catch{}setListening(false);return;}
    try{
      const rec=new SR();rec.lang='en-GB';rec.interimResults=false;rec.maxAlternatives=1;
      rec.onresult=(event:any)=>{const said=event?.results?.[0]?.[0]?.transcript||'';if(said)setDraft(said);};
      rec.onerror=()=>{setListening(false);setErr('Microphone unavailable. Check browser permissions.');};
      rec.onend=()=>setListening(false);
      recognitionRef.current=rec;rec.start();setListening(true);setErr('');
    }catch{setListening(false);setErr('Could not start the microphone.');}
  }

  const micAvailable=!!((window as any).SpeechRecognition||(window as any).webkitSpeechRecognition);
  const ttsAvailable=typeof window!=='undefined'&&'speechSynthesis' in window;

  /** Hand a reply to the Raspberry Pi so the room speaker says it out loud. */
  async function speakOnPi(text:string){
    if(!text.trim())return;
    setSpeaking(true);setSpeakerNote('');
    try{const r=await request<{message:string}>('/api/speak',{method:'POST',body:JSON.stringify({text})});setSpeakerNote(r.message);}
    catch(e){setSpeakerNote(e instanceof Error?e.message:'The speaker is unavailable');}
    finally{setSpeaking(false);}
  }

  return <>
    <SectionHead title="ELDRIS assistant" sub="Answers are built from live dashboard data. Nothing here is a medical opinion or an emergency service." action={<LiveClock/>}/>
    <div className="assistant-layout">
      <section className="panel chat-panel">
        <div className="chat-scroll" ref={scrollRef}>
          {turns.map((turn,i)=><div key={i} className={`chat-row ${turn.role==='user'?'mine':''}`}>
            {turn.role==='assistant'&&<div className="chat-avatar"><Bot size={16}/></div>}
            <div className="bubble">
              {turn.content.split('\n').map((line,j)=><p key={j}>{line}</p>)}
              {turn.role==='assistant'&&turn.source&&<em className={`chat-source ${turn.source.startsWith('llm')?'llm':''}`}>{turn.source.startsWith('llm')?'AI model':'Offline assistant'}</em>}
            </div>
          </div>)}
          {busy&&<div className="chat-row"><div className="chat-avatar"><Bot size={16}/></div><div className="bubble typing"><span/><span/><span/></div></div>}
        </div>
        {err&&<div className="form-error">{err}</div>}
        <div className="assistant-tools">
          <button type="button" className="secondary-button small" disabled={speaking||!lastAssistant} onClick={()=>speakOnPi(lastAssistant)}><Volume2 size={15}/>{speaking?'Speaking on the Pi…':'Speak the last reply on the Pi speaker'}</button>
          {speakerNote&&<span className="speak-status">{speakerNote}</span>}
        </div>
        <form className="chat-compose" onSubmit={e=>{e.preventDefault();send(draft);}}>
          <input value={draft} onChange={e=>setDraft(e.target.value)} placeholder="Ask about vitals, home climate, the robot, reminders or alerts…" maxLength={500} disabled={busy}/>
          {micAvailable&&<button type="button" className={`icon-button ${listening?'listening':''}`} onClick={toggleMic} title={listening?'Stop listening':'Ask with your voice'} aria-label={listening?'Stop listening':'Ask with your voice'}><Mic size={17}/></button>}
          <button className="primary-button" type="submit" disabled={busy||!draft.trim()} aria-label="Send message"><Send size={16}/> Send</button>
        </form>
        {ttsAvailable&&<label className="toggle chat-tts"><input type="checkbox" checked={speak} onChange={e=>setSpeak(e.target.checked)}/><Volume2 size={14}/> <span>Speak replies aloud</span></label>}
      </section>
      <aside className="panel chat-side">
        <SectionHead title="Try asking"/>
        <div className="quick-prompts">{QUICK_PROMPTS.map(q=><button key={q} onClick={()=>send(q)} disabled={busy}>{q}</button>)}</div>            <div className="side-block">
          <SectionHead title="Right now"/>
          <div className="health-row"><span>Heart rate</span><div>{num(band?.vitals.heart_rate_bpm,0,' bpm')}</div></div>
          <div className="health-row"><span>Blood oxygen</span><div>{num(band?.vitals.spo2_pct,1,'%')}</div></div>
          <div className="health-row"><span>Body temperature</span><div>{num(band?.vitals.body_temperature_c,1,' °C')}</div></div>
          <div className="health-row"><span>Home climate</span><div>{num(climate?.home.temperature_c,1,' °C')} · {num(climate?.home.humidity_pct,0,'%')}</div></div>
          <div className="health-row"><span>Robot</span><div><span className={`mini-dot ${robot?'green':'grey'}`}/>{robot?.room_name||'Unknown'}</div></div>
          <div className="health-row"><span>Falls today</span><div>{motion?String(motion.summary.falls_today):'—'}</div></div>
          <div className="health-row"><span>Hand tremor</span><div>{motion?(motion.summary.tremor_active?'Active':'None'):'—'}</div></div>
        </div>
        <div className="side-block">
          <SectionHead title="Help words"/>
          <div className="help-words">{(help?.keywords||['help','help me','please help','i need help','i have fallen']).map(word=><span className="help-word" key={word}>{word}</span>)}</div>
          <div className="health-row"><span>Chatbot</span><div><span className={`mini-dot ${help?.device.online?'green':'grey'}`}/>{help?.device.online?'Listening':'Silent'}</div></div>
          <div className="health-row"><span>Last help phrase</span><div>{help?.last_trigger?.keyword?`${help.last_trigger.keyword} · ${stamp(help.last_trigger.at)}`:'Never'}</div></div>
          {help?.recording.active&&<div className="recording-flag"><span className="dot"/>Recording · {help.recording.seconds_left}s left</div>}
          <p className="muted">Say any one of the five and the dashboard turns red, the robot is sent to the <b>{help?.patient_room||'bedroom'}</b>, and the board records what follows for the caregiver. The server keeps the transcript, not the audio.</p>
        </div>
        <div className="info-note"><ShieldCheck size={18}/><span>The assistant quotes the last readings the boards sent and never invents them. If a board is silent it says so.</span></div>
      </aside>
    </div>
  </>;
}

/* ------------------------------------------------------------- devices */
function DevicesPage({devices,robot}:{devices:DevicesPayload|null;robot:RobotLocation|null}){
  const [kind,setKind]=useState('all');
  const kinds=useMemo(()=>Array.from(new Set(devices?.items.map(d=>d.kind)||[])),[devices]);
  const shown=(devices?.items||[]).filter(d=>kind==='all'||d.kind===kind);
  const iconFor=(k:string)=>k==='camera'?Camera:k==='audio'?Volume2:k==='sensor'?Thermometer:k==='robot'?Bot:k==='hub'?Radio:k==='wearable'?HeartPulse:Command;
  return <>
    <SectionHead title="Devices" sub="One left-wrist band, one home climate sensor, the chatbot ESP32 with its microphone and speaker, two robot body cameras and the hub that ties them together." action={<select className="filter-select" value={kind} onChange={e=>setKind(e.target.value)}><option value="all">All types</option>{kinds.map(k=><option key={k} value={k}>{KIND_LABEL[k]||k}</option>)}</select>}/>
    {devices&&<div className="stats-grid">
      <Stat delay={0} label="Total devices" value={String(devices.summary.total)} detail={`${kinds.length} types`} icon={Cpu} tone="neutral"/>
      <Stat delay={1} label="Online" value={String(devices.summary.online)} detail="Reporting to the hub" icon={Wifi} tone="green"/>
      <Stat delay={2} label="Offline" value={String(devices.summary.offline)} detail="No recent report" icon={WifiOff} tone={devices.summary.offline?'amber':'neutral'}/>
      <Stat delay={3} label="Cameras" value={String(devices.summary.by_kind.camera||0)} detail="Both mounted on the robot body" icon={Camera} tone="neutral"/>
    </div>}
    <div className="panel">
      {shown.length?shown.map((d,i)=>{const Icon=iconFor(d.kind);return <div className="device-row" key={d.id} style={vars(i)}>
        <div className={`device-icon ${d.kind}`}><Icon size={18}/></div>
        <div className="grow"><b>{d.name}</b><small>{d.note}</small></div>
        <span className="device-kind">{KIND_LABEL[d.kind]||d.kind}</span>
        <span className="device-room">{d.room_name||(d.kind==='wearable'?'Worn on the wrist':'—')}</span>
        {d.address&&<span className="device-address" title="Address this ESP32 answers on">{d.address}</span>}
        {d.battery_pct!=null&&<span className={`device-battery ${d.low_battery?'low':''}`}>{d.battery_pct}%</span>}
        <StatusTag good={d.online}>{d.online?'Online':'Offline'}</StatusTag>
      </div>;}):<Empty icon={Cpu} title="No devices match" sub="Try a different device type."/>}
    </div>
    {robot&&<div className="info-note"><MapPin size={18}/><span>The robot and both of its body cameras are currently in the <b>{robot.room_name}</b>. Change it from the Robot Control page or ask the assistant.</span></div>}
  </>;
}

/* ------------------------------------------------------------ reminders */
function Reminders({reminders,refresh}:{reminders:Reminder[];refresh:()=>void}){
  const [title,setTitle]=useState(''),[category,setCategory]=useState('Hydration'),[date,setDate]=useState(()=>{const d=new Date(Date.now()+3600000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}),[err,setErr]=useState('');
  async function add(e:FormEvent){e.preventDefault();try{await request('/api/reminders',{method:'POST',body:JSON.stringify({title,category,scheduled_at:new Date(date).toISOString(),enabled:true})});setTitle('');setErr('');refresh();}catch(e){setErr(e instanceof Error?e.message:'Could not save reminder');}}
  async function change(id:number,body:any){try{await request(`/api/reminders/${id}`,{method:'PATCH',body:JSON.stringify(body)});refresh();}catch(e){setErr(e instanceof Error?e.message:'Update failed');}}
  async function remove(id:number){if(!confirm('Delete this reminder?'))return;await request(`/api/reminders/${id}`,{method:'DELETE'});refresh();}
  return <>
    <SectionHead title="Reminders & check-ins" sub="Manage prompts that the Raspberry Pi reminder service can speak."/>
    <div className="reminder-layout">
      <section className="panel"><SectionHead title="Create reminder" sub="A reminder playing does not confirm an action was completed."/>
        <form className="form-stack" onSubmit={add}>
          <label>Reminder title<input value={title} onChange={e=>setTitle(e.target.value)} placeholder="e.g. Drink a glass of water" required maxLength={100}/></label>
          <label>Category<select value={category} onChange={e=>setCategory(e.target.value)}>{['Medication','Hydration','Daily activity','Check-in','Appointment','Other'].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>Date and time<input type="datetime-local" value={date} onChange={e=>setDate(e.target.value)} required/></label>
          {err&&<div className="form-error">{err}</div>}
          <button className="primary-button" type="submit"><Plus size={17}/> Add reminder</button>
        </form>
      </section>
      <section className="panel"><SectionHead title="Scheduled reminders" sub={`${reminders.length} saved reminders`}/>
        {reminders.length?reminders.map(r=><div className="list-row" key={r.id}>
          <div className="reminder-icon"><Bell size={17}/></div>
          <div className="grow"><b>{r.title}</b><small>{r.category} · {stamp(r.scheduled_at)}</small><small>Playback status: {r.status||'scheduled'}</small></div>
          <label className="toggle"><input type="checkbox" checked={r.enabled} onChange={e=>change(r.id,{enabled:e.target.checked})}/><span>{r.enabled?'Enabled':'Paused'}</span></label>
          <button className="icon-button danger-hover" title="Delete reminder" aria-label="Delete reminder" onClick={()=>remove(r.id)}><Trash2 size={16}/></button>
        </div>):<Empty icon={Bell} title="No reminders saved" sub="Use the form to schedule a caregiver prompt."/>}
      </section>
    </div>
    <div className="info-note"><CircleHelp size={18}/><span>Playback and acknowledgement are separate states. This web app does not mark medication as taken.</span></div>
  </>;
}

/* --------------------------------------------------------------- alerts */
function Alerts({alerts,refresh}:{alerts:AlertItem[];refresh:()=>void}){
  const [filter,setFilter]=useState('all');
  async function update(id:number,status:string){try{await request(`/api/alerts/${id}`,{method:'PATCH',body:JSON.stringify({status})});refresh();}catch{}}
  const shown=alerts.filter(a=>filter==='all'||a.status===filter);
  return <>
    <SectionHead title="Caregiver alerts" sub="Review, acknowledge and resolve events raised by the wrist band and the robot." action={<select className="filter-select" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">All statuses</option><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option></select>}/>
    <div className="panel">
      {shown.length?shown.map((a,i)=><div className="alert-card" key={a.id} style={vars(i)}>
        <div className={`alert-symbol ${a.severity}`}><AlertTriangle size={20}/></div>
        <div className="grow">
          <div className="alert-title"><b>{a.type.replaceAll('_',' ')}</b><span className={`severity ${a.severity}`}>{a.severity}</span></div>
          <p>{a.description}</p><small>{stamp(a.created_at)} · {a.status}</small>
        </div>
        <div className="alert-actions">
          {a.status==='open'&&<button className="secondary-button" onClick={()=>update(a.id,'acknowledged')}><Check size={15}/> Acknowledge</button>}
          {a.status!=='resolved'&&<button className="primary-button small" onClick={()=>update(a.id,'resolved')}>Resolve</button>}
        </div>
      </div>):<Empty icon={CheckCircle2} title="No alerts match" sub="Try a different status filter."/>}
    </div>
    <div className="info-note"><ShieldCheck size={18}/><span>Alerts are best-effort prototype notifications, not a guaranteed emergency response service.</span></div>
  </>;
}

/* ------------------------------------------------------ motion & body */
const MOTION_LEVEL:Record<string,string>={critical:'Critical',high:'High',medium:'Medium',low:'Low'};
/** Bars are scaled to 1 g of hand deviation, the ceiling that matters here. */
const traceHeight=(v:number)=>Math.max(4,Math.min(100,Math.round(v*100)));
/** A centred bar: negative values fill left of the mid-line, positive right. */
function axisFill(value:number|null):CSSProperties{
  if(value==null)return {left:'50%',width:'0%'};
  const clamped=Math.max(-2,Math.min(2,value));
  const half=Math.abs(clamped)/2*50;
  return clamped>=0?{left:'50%',width:`${half}%`}:{right:'50%',width:`${half}%`};
}

/** The single wrist band: vitals, tremor state, live axes and the movement trace. */
function WearableCard({wearable,delay=0}:{wearable:Wearable;delay?:number}){
  const tremor=wearable.tremor;
  const vitals=wearable.vitals;
  const band=`${tremor.band[0]}–${tremor.band[1]} Hz`;
  return <article className="wearable-card" style={vars(delay)}>
    <header>
      <div className="wearable-head-icon"><HeartPulse size={20}/></div>
      <div className="grow"><b>{wearable.name}</b><small>{wearable.note}</small></div>
      <span className={`tremor-flag ${tremor.active?'active':'quiet'}`}>{tremor.active?<><Zap size={12}/> Tremor</>:<><Check size={12}/> Steady</>}</span>
    </header>
    <div className="motion-badges">
      <span className={`motion-badge ${wearable.online?'live':''}`}>{wearable.online?'Live':'No report'}</span>
      <span className="motion-badge">Worn on the {wearable.hand} wrist</span>
      <span className="motion-badge">{wearable.posture}</span>
      {wearable.sample_rate_hz>0&&<span className="motion-badge">{wearable.sample_rate_hz} Hz</span>}
      {wearable.battery_pct!=null&&<span className="motion-badge">Battery {wearable.battery_pct}%</span>}
    </div>
    <div className="band-vitals">
      <div className={`band-vital ${vitals.stale?'sim':''}`}><span><HeartPulse size={13}/> Heart rate</span><strong>{num(vitals.heart_rate_bpm,0,' bpm')}</strong><small>MAX30102 pulse</small></div>
      <div className={`band-vital ${vitals.stale?'sim':''}`}><span><Gauge size={13}/> Blood oxygen</span><strong>{num(vitals.spo2_pct,1,'%')}</strong><small>SpO₂ saturation</small></div>
      <div className={`band-vital ${vitals.stale?'sim':''}`}><span><Thermometer size={13}/> Body temperature</span><strong>{num(vitals.body_temperature_c,1,'°C')}</strong><small>Skin-contact sensor</small></div>
    </div>
    <div className="wearable-body">
      <div className="wearable-metric"><span>Hand movement</span><strong>{num(wearable.activity_g,2,' g')}</strong><small>Deviation over the last second</small></div>
      <div className="wearable-metric"><span>Tremor band</span><strong>{tremor.active?num(tremor.frequency_hz,1,' Hz'):'—'}</strong><small>{tremor.active?`Amplitude ${num(tremor.amplitude_g,3,' g')}`:`Watching ${band}`}</small></div>
    </div>
    <div className={`trace ${tremor.active?'alarm':wearable.activity_g<0.05?'calm':''}`} title="Recent hand movement">
      {wearable.trace.length?wearable.trace.map((v,i)=><i key={i} style={{height:`${traceHeight(v)}%`}}/>):<i style={{height:'4%'}}/>}
    </div>
    <div className="axis-list">
      {(['x','y','z'] as const).map(axis=><div className="axis-row" key={axis}>
        <span>{axis==='y'?'Y · fall axis':`${axis.toUpperCase()} axis`}</span>
        <span className="axis-bar"><span className="axis-fill" style={axisFill(wearable.axes[axis])}/></span>
        <b>{wearable.axes[axis]==null?'—':`${wearable.axes[axis]!.toFixed(2)} g`}</b>
      </div>)}
    </div>
    <div className="panel-foot">
      {wearable.fall.last_at?`Last fall from this band: ${stamp(wearable.fall.last_at)}`:'No fall from this band since it started reporting.'}
      {wearable.fall.suspected?' · Impact being checked now':''}
    </div>
  </article>;
}

function MotionPage({motion,emergency,onRefresh,onAcknowledge}:{motion:MotionPayload|null;emergency:Emergency|null;onRefresh:()=>void;onAcknowledge:()=>void}){
  const summary=motion?.summary,settings=motion?.settings;
  return <>
    <SectionHead title="Body & motion" sub="One ESP32 #1, worn on the left wrist. The MPU6050 watches movement and falls, the MAX30102 reads heart rate and blood oxygen, and an MLX90614 reads body temperature." action={<LiveClock/>}/>
    {emergency?.active&&<div className="emergency-panel panel" style={{marginBottom:'18px'}}>
      <SectionHead title={emergency.headline} sub={`${emergency.detail} Use Acknowledge in the banner above when this has been handled.`} action={<span className="emergency-chip"><span className="dot"/>Active</span>}/>
    </div>}
    {!summary?.wearables_online&&<div className="info-note"><Waves size={18}/><span>ESP32 #1 is not reporting. Power the band and check <code>API_URL</code> plus <code>DEVICE_API_KEY</code>; the numbers below are the last readings it sent, not new ones.</span></div>}
    <div className="hand-row">
      <span className="hand-label"><HeartPulse size={14}/> One band · left wrist</span>
      <span className="hand-note">Oxygen, body temperature, heart rate, tremor and falls all come from this one band. There is no right-wrist band.</span>
    </div>
    {summary&&<div className="stats-grid">
      <Stat delay={0} label="Heart rate" value={num(summary.heart_rate_bpm,0,' bpm')} detail={summary.vitals_updated_at?`Last packet ${stamp(summary.vitals_updated_at)}`:'No vitals packet yet'} icon={HeartPulse} tone={heartTone(summary.heart_rate_bpm)}/>
      <Stat delay={1} label="Blood oxygen" value={num(summary.spo2_pct,1,'%')} detail="SpO₂ saturation" icon={Gauge} tone={spo2Tone(summary.spo2_pct)}/>
      <Stat delay={2} label="Body temperature" value={num(summary.body_temperature_c,1,'°C')} detail="Contact temperature" icon={Thermometer} tone={tempTone(summary.body_temperature_c)}/>
      <Stat delay={3} label="Tremor now" value={summary.tremor_active?`${summary.tremor_active} active`:'None'} detail={`Sustained ${summary.tremor_band_hz[0]}–${summary.tremor_band_hz[1]} Hz band`} icon={Waves} tone={summary.tremor_active?'red':'green'}/>
      <Stat delay={4} label="Falls today" value={String(summary.falls_today)} detail={summary.last_fall_at?`Most recent ${stamp(summary.last_fall_at)}`:'No fall recorded'} icon={TriangleAlert} tone={summary.falls_today?'red':'green'}/>
      <Stat delay={5} label="Hand movement" value={num(summary.activity_g,2,' g')} detail={`${summary.wearables_online} of ${summary.wearables_total} band reporting`} icon={Activity} tone="neutral"/>
    </div>}
    <div className="motion-grid">
      {motion?.wearables.length?motion.wearables.map((w,i)=><WearableCard key={w.id} wearable={w} delay={i}/>):<Empty icon={Waves} title="No wrist band yet" sub="The band appears here once it posts to /api/motion."/>}
    </div>
    <div className="control-layout">
      <section className="panel">
        <SectionHead title="Detected events" sub="Newest first. Falls and strong tremors also raise caregiver alerts."/>
        {motion?.events.length?motion.events.map((e,i)=><div className="motion-event" key={e.id} style={vars(i)}>
          <div className={`motion-event-icon ${e.kind}`}>{e.kind==='fall'?<TriangleAlert size={18}/>:e.kind==='tremor'?<Waves size={18}/>:<Zap size={18}/>}</div>
          <div className="grow">
            <b>{e.kind.replaceAll('_',' ')} · {e.device_name}</b>
            <p>{e.detail}</p>
            <small>{stamp(e.created_at)} · {MOTION_LEVEL[e.level]||e.level} severity · detector on the wrist band</small>
          </div>
          <span className={`severity ${e.level}`}>{MOTION_LEVEL[e.level]||e.level}</span>
        </div>):<Empty icon={Waves} title="No motion events yet" sub="A fall, a knock or a sustained tremor will be listed here."/>}
      </section>
      <section className="panel">
        <SectionHead title="Detection thresholds" sub="The band is judged with these values right now." action={          <button className="icon-button" title="Refresh" aria-label="Refresh motion data" onClick={onRefresh}><RefreshCw size={16}/></button>}/>
        {settings?<>
          <div className="settings-line"><span>Tremor band</span><b>{settings.tremor_band_hz[0]}–{settings.tremor_band_hz[1]} Hz</b></div>
          <div className="settings-line"><span>Tremor amplitude floor</span><b>{settings.tremor_amplitude_g} g</b></div>
          <div className="settings-line"><span>Hold time before it counts</span><b>{settings.tremor_sustain_s} s</b></div>
          <div className="settings-line"><span>Fall impact on the Y axis</span><b>{settings.fall_impact_g} g</b></div>
          <div className="settings-line"><span>Orientation change that confirms a fall</span><b>{settings.fall_tilt_deg}°</b></div>
          <div className="settings-line"><span>Confirmation window</span><b>{settings.fall_confirm_s} s</b></div>
          <div className="settings-line"><span>Sample rate the band should use</span><b>{settings.sample_rate_hz} Hz</b></div>
        </>:<p className="muted">Start the backend to load the current thresholds.</p>}
        <div className="demo-row">
          <span className="demo-note">Detection runs on the server, so these thresholds are used as soon as they are saved in <code>backend/.env</code> — no reflashing needed. Real falls and strong tremors also raise caregiver alerts.</span>
        </div>
      </section>
    </div>
    <div className="info-note"><ShieldCheck size={18}/><span>A fall, tremor or out-of-range vital is an alert for a caregiver. It is not a diagnosis, and this prototype is not a guaranteed emergency-response service.</span></div>
  </>;
}

/* --------------------------------------------------------------- camera */
function CameraPage({status:s,robot,camera,onRefresh}:{status:Status|null;robot:RobotLocation|null;camera:CameraPayload|null;onRefresh:()=>void}){
  const sources=camera?.sources||[];
  const [selected,setSelected]=useState('');
  const active=sources.find(x=>x.id===selected)||sources[0]||null;
  useEffect(()=>{if(!selected&&sources.length)setSelected(sources[0].id);},[sources.length,selected]);
  return <>
    <SectionHead title="Body cameras" sub="Two ESP32-CAM boards, front and rear, both mounted on the robot body so the view moves with it. No identity recognition and no stored frames." action={<button className="icon-button" title="Refresh cameras" aria-label="Refresh cameras" onClick={onRefresh}><RefreshCw size={17}/></button>}/>
    <div className="camera-grid">
      <section className="panel camera-main">
        <div className="camera-main-head">
          <CameraTabs sources={sources} active={active} onPick={setSelected}/>
          {robot&&<span className="drive-camera-note"><MapPin size={13}/> {robot.room_name}</span>}
        </div>
        <CameraLive source={active}/>
        <div className="camera-sources">
          <span className="privacy-chip"><ShieldCheck size={14}/> No frames stored</span>
          {camera&&<span className={`motion-badge ${camera.live?'live':''}`}>{camera.stream_configured?`${camera.live} of ${sources.length} ESP32-CAM boards online`:'No camera address configured yet'}</span>}
        </div>
      </section>
      <section className="panel">
        <SectionHead title="Presence status"/>
        <div className="presence-state"><div className="presence-icon"><Eye size={22}/></div><div><b>Camera boards on the body</b><small>Each board streams straight to this server over Wi-Fi</small></div></div>
        <div className="health-row"><span>Robot location</span><b className="small-value">{robot?.room_name||'Unknown'}</b></div>
        {sources.map(source=><div className="health-row" key={source.id}><span>{source.name}</span><div><span className={`mini-dot ${source.online?'green':'grey'}`}/>{source.ip||'no address'}</div></div>)}
        <div className="health-row"><span>Streams healthy</span><StatusTag good={!!camera&&camera.live>0}>{camera?`${camera.live} of ${sources.length}`:'—'}</StatusTag></div>
        <div className="health-row"><span>Privacy mode</span><StatusTag good>Enabled</StatusTag></div>
      </section>
    </div>
    {camera&&!camera.stream_configured&&<div className="info-note"><Video size={18}/><span><b>To bring the cameras live:</b> flash the two sketches in <code>hardware/esp32_cam_front</code> and <code>hardware/esp32_cam_rear</code>, then put each board&apos;s IP in <code>backend/.env</code>: <code>CAMERA_ESP_FRONT_IP=192.168.1.51</code> and <code>CAMERA_ESP_REAR_IP=192.168.1.52</code>. Restart the API and both views appear.</span></div>}
    <div className="info-note"><ShieldCheck size={18}/><span>Frames are relayed, not archived: this build writes no images to disk and keeps only the last stream state in memory.</span></div>
  </>;
}

/* -------------------------------------------------------------- history */
function History({events}:{events:EventItem[]}){
  const [search,setSearch]=useState(''),[category,setCategory]=useState('all');
  const categories=useMemo(()=>Array.from(new Set(events.map(e=>e.category))),[events]);
  const shown=events.filter(e=>(category==='all'||e.category===category)&&`${e.message} ${e.category}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <SectionHead title="Activity history" sub="A chronological record of robot and caregiver actions."/>
    <div className="panel">
      <div className="history-tools">
        <div className="searchbox"><Search size={17}/><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search activity..."/></div>
        <select className="filter-select" value={category} onChange={e=>setCategory(e.target.value)}><option value="all">All categories</option>{categories.map(c=><option key={c}>{c}</option>)}</select>
      </div>
      <div className="history-table">
        <div className="table-head"><span>Event</span><span>Category</span><span>Timestamp</span></div>
        {shown.map(e=><div className="table-row" key={e.id}><b>{e.message}</b><span className="category-chip">{e.category}</span><time>{stamp(e.created_at)}</time></div>)}
        {!shown.length&&<Empty icon={Activity} title="No matching events" sub="Try a different search or category."/>}
      </div>
    </div>
  </>;
}

/* ------------------------------------------------------------- settings */
function Preference({title,desc,checked=true}:{title:string;desc:string;checked?:boolean}){return <div className="preference"><div><b>{title}</b><small>{desc}</small></div><span className={`preference-state ${checked?'on':'off'}`}>{checked?'On':'Not active'}</span></div>;}

function Settings({status:s,refresh,devices,climate,guest,onSignIn,onSignOut}:{status:Status|null;refresh:()=>void;devices?:DevicesPayload|null;climate?:ClimatePayload|null;guest:boolean;onSignIn:()=>void;onSignOut:()=>void}){
  return <>
    <SectionHead title="System settings" sub="Connection diagnostics and privacy preferences for the prototype."/>
    <div className="settings-grid">
      <section className="panel"><SectionHead title="Robot connection" sub="Every board is reached directly over Wi-Fi; the backend reports what answers."/>
        <Health label="Drive ESP32 (#2)" value={s?.drive_connected?'Online':'Offline'} good={!!s?.drive_connected}/>
        <Health label="Drive address" value={s?.drive_url||'not set'} good={!!s?.drive_url}/>
        <Health label="Backend API" value={s?'Connected':'Unavailable'} good={!!s}/>
        <Health label="Motion detection" value="Server-side" good/>
        <Health label="Home climate sensor" value={climate?.home.online?'Online':'No report'} good={!!climate?.home.online}/>
        <Health label="Devices tracked" value={devices?`${devices.summary.online} of ${devices.summary.total} online`:'Unavailable'} good={!!devices}/>
        <button className="secondary-button full-button" onClick={refresh}><RefreshCw size={16}/> Refresh diagnostics</button>
      </section>
      <section className="panel"><SectionHead title="Caregiver access" sub="The dashboard opens as a guest; an account is optional."/>
        <div className="preference"><div><b>{guest?'Guest session':'Signed-in caregiver'}</b><small>{guest?'Local access without a name or password. Sign in to use a named account.':'Your named session is stored in an encrypted cookie.'}</small></div><span className={`preference-state ${guest?'off':'on'}`}>{guest?'Guest':'Active'}</span></div>
        {guest?<button className="primary-button full-button" onClick={onSignIn}><LockKeyhole size={16}/> Sign in or create an account</button>:<button className="secondary-button full-button" onClick={onSignOut}>Return to guest session</button>}
        <Preference title="Local processing only" desc="Vitals, climate and presence stay on the Raspberry Pi." checked/>
      </section>
      <section className="panel"><SectionHead title="Reminder preferences"/>
        <Preference title="Caregiver check-ins" desc="Schedule prompts from the Reminders page."/>
        <Preference title="Playback reporting" desc="Playback state depends on the Raspberry Pi service."/>
        <Preference title="Alert notifications" desc="Review open alerts in the Caregiver Alerts page."/>
      </section>
      <section className="panel"><SectionHead title="Camera privacy"/>
        <Preference title="Do not store camera frames" desc="Default privacy posture; no image storage is implemented." checked/>
        <Preference title="Two ESP32-CAM boards on the robot body" desc="The front and rear cameras move with the robot." checked/>
        <Preference title="Identity recognition" desc="Not implemented." checked={false}/>
      </section>
      <section className="panel"><SectionHead title="Troubleshooting"/>
        <div className="trouble-item"><b>Wrist band not reporting</b><p>Check ESP32 #1&apos;s power and 2.4 GHz Wi-Fi, that API_URL ends in /api/motion, and that DEVICE_API_KEY matches the server.</p></div>
        <div className="trouble-item"><b>Drive ESP32 offline</b><p>The server could not reach DRIVE_ESP_IP on port 80. Open http://&lt;drive-ip&gt;/api/status in a browser on the same network; if that fails, check the board&apos;s serial monitor for its IP.</p></div>
        <div className="trouble-item"><b>Camera unavailable</b><p>Set CAMERA_ESP_FRONT_IP and CAMERA_ESP_REAR_IP, then open http://&lt;cam-ip&gt;:81/stream. ESP32-CAM boards only join 2.4 GHz networks.</p></div>
        <div className="trouble-item"><b>Dashboard cannot connect</b><p>Start Flask on port 5000, keep the Vite proxy enabled, and check the backend terminal for errors.</p></div>
      </section>
    </div>
    <div className="info-note"><ShieldCheck size={18}/><span>Authenticated accounts remain available. Use HTTPS, a strong SESSION_SECRET, and restricted network access before deployment beyond a trusted prototype LAN.</span></div>
  </>;
}

function Empty({icon:Icon,title,sub}:{icon:any;title:string;sub:string}){return <div className="empty"><Icon size={24}/><b>{title}</b><span>{sub}</span></div>;}
function Health({label,value,good}:{label:string;value:string;good:boolean}){return <div className="health-row"><span>{label}</span><div><span className={`mini-dot ${good?'green':'grey'}`}/>{value}</div></div>;}
function Timeline({events}:{events:EventItem[]}){return events.length?<div className="timeline">{events.map(e=><div className="timeline-item" key={e.id}><span className="timeline-dot"/><div><b>{e.message}</b><small>{stamp(e.created_at)}</small></div></div>)}</div>:<Empty icon={Activity} title="No activity yet" sub="System events will be listed here."/>;}

export default App;
