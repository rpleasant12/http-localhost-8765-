"""Premium marketing page (pricing.html).

Standalone design supplied by the site owner (v2, 2026-10-03) - its own CSS
and nav rather than website._page(), so the render() here is the whole
document. v2 adds the three billing plans (monthly / 6-month / annual; the
CTAs carry ?plan= which member.js forwards to the worker's checkout) and
moves Education + Field Guide into the premium column (both pages are
hard-gated like the four weather centers).
Build-time substitutions:
  - window.TWN_WORKER stamped from config.MEMBER_WORKER_URL ("" = dormant).
  - the gated-center links point at the worker's /p/<page> endpoints
    when membership is configured (the public static copies are locked
    shells; premium visitors are served the real page by the worker).
The .src CSS rule and the memStrip painter below are template glue the
owner's raw file doesn't carry (the footer strip and small print rely on
them) - keep them when importing a new owner revision.
"""

_PREM_NAV_PAGES = ("severe.html", "storms.html", "tropical.html", "winter.html")

TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Premium - Tennessee Weather Network</title>
<meta name="description" content="Tennessee Weather Network Premium - advanced severe weather, tropical, winter, AI model maps, long-range analysis and an ad-free experience for $4.99/month."/>
<meta name="theme-color" content="#0e1117"/>
<link rel="manifest" href="manifest.webmanifest"/>
<link rel="apple-touch-icon" href="icon-192.png"/>
<style>
:root{
  color-scheme:dark;
  --bg:#0e1117;
  --card:#161b26;
  --card2:#10151f;
  --line:rgba(255,255,255,.09);
  --dim:#9aa4b2;
  --acc:#4da3ff;
  --gold:#ffd54f;
  --green:#70d68a;
}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:var(--bg);color:#e8edf4}
a{color:var(--acc);text-decoration:none}
.wrap{max-width:1100px;margin:0 auto;padding:14px}
nav{position:sticky;top:0;z-index:50;background:rgba(14,17,23,.96);backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
nav .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:10px 14px}
nav .brand{font-weight:800;font-size:18px;margin-right:auto;white-space:nowrap}
nav .brand span{color:var(--acc)}
nav a.pg{color:#cdd7e4;font-size:14px;padding:4px 8px;border-radius:6px}
nav a.pg:hover,nav a.pg.on{background:#1d2432;color:#fff}

.hero{text-align:center;padding:42px 10px 22px}
.hero .eyebrow{color:var(--gold);font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:13px}
.hero h1{margin:8px 0;font-size:clamp(30px,6vw,52px);line-height:1.05}
.hero h1 span{color:var(--acc)}
.hero p{max-width:720px;margin:12px auto 0;color:var(--dim);font-size:17px;line-height:1.6}

.price-card{
  max-width:560px;margin:10px auto 34px;background:linear-gradient(180deg,#192335,#161b26);
  border:1px solid rgba(77,163,255,.35);border-radius:18px;padding:28px;
  box-shadow:0 14px 45px rgba(0,0,0,.25);text-align:center
}
.badge{display:inline-block;background:rgba(255,213,79,.12);color:var(--gold);border:1px solid rgba(255,213,79,.28);padding:5px 11px;border-radius:999px;font-size:12px;font-weight:800}
.price{font-size:54px;font-weight:900;margin:12px 0 0}
.price small{font-size:16px;color:var(--dim);font-weight:500}
.cancel{color:var(--dim);font-size:13px;margin:4px 0 18px}
.cta{
  display:inline-block;background:#2b80ff;color:#fff;border-radius:10px;
  padding:12px 28px;font-size:17px;font-weight:800;border:0;cursor:pointer;
  box-shadow:0 5px 18px rgba(43,128,255,.22)
}
.cta:hover{background:#3d8cff}
.login-note{margin-top:13px;color:var(--dim);font-size:12.5px}

.section-title{text-align:center;margin:38px 0 16px}
.section-title h2{margin:0;font-size:26px}
.section-title p{margin:6px 0;color:var(--dim)}

.compare{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:28px}
.plans{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:10px 0 34px}
.billing{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;text-align:center;position:relative}
.billing.featured{border-color:rgba(77,163,255,.55);box-shadow:0 8px 30px rgba(0,0,0,.2)}
.billing .price{font-size:38px}
.billing .term{color:var(--dim);font-size:13px;margin:4px 0 12px}
.billing .save{color:var(--green);font-weight:800;font-size:13px;min-height:20px}
.billing .cta{font-size:14px;padding:10px 18px;margin-top:14px}
.premium-lock{display:inline-block;color:var(--gold);font-size:11px;font-weight:800;border:1px solid rgba(255,213,79,.28);background:rgba(255,213,79,.08);padding:3px 7px;border-radius:999px;margin-left:6px}
@media(max-width:800px){.plans{grid-template-columns:1fr}.compare{grid-template-columns:1fr}}
.plan{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
.plan.premium{border-color:rgba(77,163,255,.45);box-shadow:0 8px 28px rgba(0,0,0,.18)}
.plan h3{margin:0 0 5px;font-size:21px}
.plan .sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.plan ul{list-style:none;padding:0;margin:0}
.plan li{padding:9px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.4}
.plan li:first-child{border-top:0}
.check{color:var(--green);font-weight:900;margin-right:7px}
.star{color:var(--gold);font-weight:900;margin-right:7px}

.feature-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.feature{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:19px}
.feature .icon{font-size:27px}
.feature h3{margin:8px 0 6px;font-size:17px}
.feature p{margin:0;color:var(--dim);font-size:13.5px;line-height:1.55}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.tag{background:#10151f;border:1px solid var(--line);border-radius:7px;padding:4px 7px;color:#cdd7e4;font-size:11px}

.support{margin:30px 0;background:#10151f;border:1px solid var(--line);border-radius:14px;padding:20px;text-align:center}
.support h3{margin:0 0 6px}
.support p{margin:0;color:var(--dim);font-size:13.5px;line-height:1.55}

.faq{margin-bottom:30px}
details{background:var(--card);border:1px solid var(--line);border-radius:11px;margin:8px 0;padding:13px 15px}
summary{cursor:pointer;font-weight:700}
details p{color:var(--dim);font-size:13.5px;line-height:1.55;margin:10px 0 2px}

.status{display:none;max-width:760px;margin:0 auto 20px;padding:11px 14px;border-radius:10px;background:#10151f;border:1px solid var(--line);text-align:center;color:#cdd7e4}
.status.show{display:block}
.status.premium{border-color:rgba(112,214,138,.35);color:var(--green)}

.src{color:var(--dim);font-size:12.5px}

footer{text-align:center;color:var(--dim);font-size:12.5px;padding:20px 8px 30px;line-height:1.8}

#navBtn{display:none}
#navDrawer{display:none}
@media(max-width:640px){
  #navBtn{display:block;background:#1d2432;color:#e8edf4;border:1px solid var(--line);border-radius:8px;font-size:18px;padding:6px 12px;cursor:pointer;position:absolute;right:10px;top:8px;z-index:60}
  #navDrawer{display:block;position:fixed;inset:0;z-index:9999;background:rgba(5,8,12,.7);backdrop-filter:blur(2px)}
  #navDrawer[hidden]{display:none}
  #navDrawer .dHead,#navDrawer .dGroup{background:var(--card);border-bottom:1px solid var(--line)}
  #navDrawer .dHead{padding:14px 16px;display:flex;justify-content:space-between;font-weight:800}
  #navDrawer .dHead button{background:none;border:0;color:var(--dim);font-size:26px}
  #navDrawer .dGroup{padding:6px 12px}
  #navDrawer .dGroup b{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin:10px 4px 4px}
  #navDrawer a.pg{display:block;padding:10px;color:#cdd7e4;border-radius:8px}
  nav .wrap{padding-right:58px;overflow:hidden}
  nav a.pg{display:none}
  .compare,.feature-grid{grid-template-columns:1fr}
  .hero{padding-top:30px}
  .price-card{padding:23px 17px}
}
</style>
</head>
<body>

<nav>
  <div class="wrap">
    <a class="brand" href="weather.html">🌧️ <span>Tennessee Weather Network</span></a>
    <a class="pg" href="weather.html">Home</a>
    <a class="pg" href="radar.html">Radar</a>
    <a class="pg" href="forecast.html">Forecast</a>
    <a class="pg" href="models.html">Models</a>
    <a class="pg" href="severe.html">Severe ⭐</a>
    <a class="pg" href="storms.html">Storms ⭐</a>
    <a class="pg" href="tropical.html">NHC ⭐</a>
    <a class="pg" href="winter.html">Winter ⭐</a>
    <a class="pg on" href="pricing.html">⭐ Premium</a>
    <a class="pg" href="member.html">Account</a>
  </div>
</nav>

<button id="navBtn" aria-label="Open menu">☰</button>
<div id="navDrawer" hidden>
  <div class="dHead">Tennessee Weather Network <button id="navClose">×</button></div>
  <div class="dGroup"><b>Live & Forecast</b>
    <a class="pg" href="weather.html">Home</a><a class="pg" href="radar.html">Radar</a><a class="pg" href="forecast.html">Forecast</a>
  </div>
  <div class="dGroup"><b>Models</b>
    <a class="pg" href="models.html">Models</a><a class="pg" href="hrrr.html">HRRR · RRFS</a><a class="pg" href="cfsv2.html">CFSv2 · Long Range</a>
  </div>
  <div class="dGroup"><b>Storms & Seasons</b>
    <a class="pg" href="severe.html">Severe ⭐</a><a class="pg" href="storms.html">Storms ⭐</a><a class="pg" href="tropical.html">NHC ⭐</a><a class="pg" href="winter.html">Winter Forecast ⭐</a>
  </div>
  <div class="dGroup"><b>Support</b>
    <a class="pg on" href="pricing.html">⭐ Premium</a><a class="pg" href="member.html">Account</a>
  </div>
</div>

<main class="wrap">
  <header class="hero">
    <div class="eyebrow">⭐ Tennessee Weather Network Premium</div>
    <h1>More weather.<br><span>Less noise.</span></h1>
    <p>Keep the core weather tools free. Premium gives serious weather watchers deeper storm analysis, advanced model guidance, long-range tools and an ad-free experience.</p>
  </header>

  <div id="accountStatus" class="status"></div>

  <section class="price-card">
    <span class="badge">PREMIUM MEMBERSHIP</span>
    <div class="price">Choose your plan</div>
    <div class="cancel">All plans include the same Premium features · cancel anytime</div>
    <div class="login-note">Already have an account? <a href="member.html">Sign in to your account</a>.</div>
  </section>

  <section class="plans">
    <article class="billing">
      <h3>Monthly</h3>
      <div class="price">$4.99 <small>/ month</small></div>
      <div class="term">Billed monthly</div>
      <div class="save">Flexible · cancel anytime</div>
      <a class="cta" href="member.html?plan=monthly">⭐ Choose Monthly</a>
    </article>
    <article class="billing featured">
      <span class="badge">1 MONTH FREE</span>
      <h3>6 Months</h3>
      <div class="price">$24.95 <small>/ 6 months</small></div>
      <div class="term">Pay for 5 months, get the 6th free</div>
      <div class="save">Save $4.99</div>
      <a class="cta" href="member.html?plan=6mo">⭐ Choose 6 Months</a>
    </article>
    <article class="billing">
      <span class="badge">BEST VALUE · 2 MONTHS FREE</span>
      <h3>Annual</h3>
      <div class="price">$49.90 <small>/ year</small></div>
      <div class="term">Pay for 10 months, get 2 free</div>
      <div class="save">Save $9.98</div>
      <a class="cta" href="member.html?plan=annual">⭐ Choose Annual</a>
    </article>
  </section>

  <div class="section-title">
    <h2>Free vs. Premium</h2>
    <p>The core weather service stays free.</p>
  </div>

  <section class="compare">
    <article class="plan">
      <h3>🌤️ Free</h3>
      <div class="sub">$0 forever</div>
      <ul>
        <li><span class="check">✓</span>Current conditions & forecasts</li>
        <li><span class="check">✓</span>Live radar + future cast</li>
        <li><span class="check">✓</span>National weather</li>
        <li><span class="check">✓</span>GFS, NAM, HRRR & ECMWF staples</li>
        <li><span class="check">✓</span>Fronts, fire, rivers & climate centers</li>
        <li><span class="star">★</span>Education & Field Guide <span class="premium-lock">PREMIUM</span></li>
        <li><span class="check">✓</span>Free account access</li>
        <li>• Ad-supported</li>
      </ul>
    </article>

    <article class="plan premium">
      <h3>⭐ Premium</h3>
      <div class="sub">$4.99/month · cancel anytime</div>
      <ul>
        <li><span class="star">★</span>Everything in Free</li>
        <li><span class="star">★</span>Severe & Storms Center</li>
        <li><span class="star">★</span>NHC Tropical Center</li>
        <li><span class="star">★</span>Winter Weather Center</li>
        <li><span class="star">★</span>Advanced AI model maps</li>
        <li><span class="star">★</span>CFSv2 long-range analysis</li>
        <li><span class="star">★</span>Ad-free everywhere</li>
        <li><span class="star">★</span>New Premium tools as released</li>
      </ul>
    </article>
  </section>

  <div class="section-title">
    <h2>What's included with Premium?</h2>
    <p>Built for people who want to go deeper than the basic forecast.</p>
  </div>

  <section class="feature-grid">
    <article class="feature">
      <div class="icon">⛈️</div>
      <h3>Severe & Storms Center</h3>
      <p>Go beyond the basic forecast with SPC outlooks, mesoscale discussions, storm analysis and severe-weather guidance.</p>
      <div class="tags"><span class="tag">SPC Outlooks</span><span class="tag">Mesoscale</span><span class="tag">Storm Archive</span></div>
    </article>

    <article class="feature">
      <div class="icon">🌀</div>
      <h3>NHC Tropical Center</h3>
      <p>Follow tropical systems with live storm tracking, official NHC information, sea-surface temperatures and marine conditions.</p>
      <div class="tags"><span class="tag">Live Tracking</span><span class="tag">SST</span><span class="tag">Marine</span></div>
    </article>

    <article class="feature">
      <div class="icon">❄️</div>
      <h3>Winter Weather Center</h3>
      <p>Explore snow and ice guidance, HRRR winter maps and seasonal outlook information during the cold-weather season.</p>
      <div class="tags"><span class="tag">Snow</span><span class="tag">Ice</span><span class="tag">HRRR</span></div>
    </article>

    <article class="feature">
      <div class="icon">🤖</div>
      <h3>Advanced AI Model Maps</h3>
      <p>Compare next-generation weather guidance and atmospheric products, including GraphCast, Pangu, Aurora and FourCastNet.</p>
      <div class="tags"><span class="tag">GraphCast</span><span class="tag">Pangu</span><span class="tag">Aurora</span><span class="tag">FourCastNet</span></div>
    </article>

    <article class="feature">
      <div class="icon">📅</div>
      <h3>Long-Range Analysis</h3>
      <p>Access the CFSv2 monthly outlook library for extended-range weather analysis and planning.</p>
      <div class="tags"><span class="tag">CFSv2</span><span class="tag">Monthly Outlooks</span></div>
    </article>

    <article class="feature">
      <div class="icon">📚</div>
      <h3>Weather Education <span class="premium-lock">PREMIUM</span></h3>
      <p>Learn forecasting concepts, weather terminology, model interpretation and severe-weather fundamentals through the TNWN education center.</p>
      <div class="tags"><span class="tag">Forecasting</span><span class="tag">Models</span><span class="tag">Weather Basics</span></div>
    </article>

    <article class="feature">
      <div class="icon">🧭</div>
      <h3>Field Guide <span class="premium-lock">PREMIUM</span></h3>
      <p>Use the TNWN field guide for clouds, storms, weather observations, identification and practical weather reference material.</p>
      <div class="tags"><span class="tag">Clouds</span><span class="tag">Storms</span><span class="tag">Observation</span></div>
    </article>

    <article class="feature">
      <div class="icon">🚫</div>
      <h3>Ad-Free Weather</h3>
      <p>No advertising while you use the network. Your Premium account is recognized across the site.</p>
      <div class="tags"><span class="tag">Ad-Free</span><span class="tag">Account-Wide</span></div>
    </article>
  </section>

  <section class="support">
    <h3>🌎 Help keep the core weather service free</h3>
    <p>Premium helps support Tennessee Weather Network while keeping the basic weather experience available to everyone.</p>
  </section>

  <div class="section-title">
    <h2>Coming soon</h2>
    <p>Additional Premium features planned for future releases.</p>
  </div>

  <section class="feature-grid">
    <article class="feature"><div class="icon">📍</div><h3>Custom Locations</h3><p>Save the places that matter most to you.</p></article>
    <article class="feature"><div class="icon">🔔</div><h3>Personalized Alerts</h3><p>More personalized weather notifications and alerts.</p></article>
    <article class="feature"><div class="icon">🗂️</div><h3>Historical Archive</h3><p>Expanded access to historical weather information.</p></article>
    <article class="feature"><div class="icon">🚀</div><h3>More Premium Tools</h3><p>New advanced weather products will be added as the network grows.</p></article>
  </section>

  <div class="section-title">
    <h2>Frequently asked questions</h2>
  </div>

  <section class="faq">
    <details><summary>Is the basic weather service still free?</summary><p>Yes. Tennessee Weather Network keeps the core weather experience free, including current conditions, forecasts, radar and core weather information. Education and Field Guide are Premium features.</p></details>
    <details><summary>How much is Premium?</summary><p>Choose from $4.99 monthly, $24.95 for 6 months with 1 month free, or $49.90 annually with 2 months free.</p></details>
    <details><summary>Can I cancel?</summary><p>Yes. Premium is intended to be cancellable at any time through the account/subscription system.</p></details>
    <details><summary>Does Premium remove ads?</summary><p>Yes. Active Premium accounts are recognized by the site and Premium members receive the ad-free benefit.</p></details>
    <details><summary>Do I need an account?</summary><p>Yes. A member account is used to identify your Premium subscription and apply Premium access across the site.</p></details>
  </section>
</main>

<footer>
  <div id="memStrip"></div>
  Tennessee Weather Network · Free weather for everyone. Advanced weather for people who need more.<br/>
  Data and products are provided for informational and educational purposes.
</footer>

<script>
window.TWN_WORKER = __TWN_WORKER__;
</script>
<script src="member.js"></script>
<script>
document.addEventListener("DOMContentLoaded", function(){
  const nb=document.getElementById("navBtn"), nd=document.getElementById("navDrawer");
  const close=document.getElementById("navClose");
  function drawer(open){
    nd.hidden=!open;
    document.body.style.overflow=open?"hidden":"";
    nb.setAttribute("aria-expanded",open?"true":"false");
  }
  if(nb&&nd){
    nb.onclick=()=>drawer(nd.hidden);
    close.onclick=()=>drawer(false);
    nd.onclick=e=>{if(e.target===nd)drawer(false);};
    nd.querySelectorAll("a").forEach(a=>a.onclick=()=>drawer(false));
  }

  const status=document.getElementById("accountStatus");
  if(window.TWN&&TWN.member&&window.TWN_WORKER){
    TWN.member.refreshMe().then(function(me){
      if(status){
        if(me&&me.premiumActive){
          status.className="status show premium";
          status.textContent="⭐ Premium is active on this account.";
        }else if(me&&me.member){
          status.className="status show";
          status.textContent="You're signed in. Upgrade below to activate Premium.";
        }
      }
      var st=document.getElementById("memStrip");
      if(st){
        if(me&&me.member){
          st.innerHTML="<span class='src' style='color:#8ab4f8'>★ "+TWN.member.esc(me.email)
            +(me.premiumActive?" · ⭐ Premium active":" · free account")
            +" · <a href='member.html'>account</a>"
            +(me.admin?" · <a href='admin.html'>admin</a>":"")+"</span><br/>";
        }else{
          st.innerHTML="<span class='src'>⭐ <a href='pricing.html'>Go premium: weather centers, AI model maps, ad-free - $4.99/mo</a></span><br/>";
        }
      }
    }).catch(function(){});
  }
});
</script>
</body>
</html>
'''


def render(worker_url):
    """Stamp the worker origin and re-point the gated-center links at the
    worker's /p/<page> endpoints (all occurrences: desktop nav + drawer)."""
    doc = TEMPLATE.replace("__TWN_WORKER__", _json(worker_url or ""))
    if worker_url:
        base = worker_url.rstrip("/") + "/p/"
        for p in _PREM_NAV_PAGES:
            doc = doc.replace('href="%s"' % p, 'href="%s%s"' % (base, p))
    return doc


def _json(v):
    import json
    return json.dumps(v)
