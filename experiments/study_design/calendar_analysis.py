"""Exact calendar weighting and prespecified day-level uncertainty calculations."""
from datetime import date
from fractions import Fraction
import math
import statistics

from .decision import require
from .precision import T95


def exact(value):
    require(type(value) in (int,Fraction),'exact integer/rational day contributions required')
    result=Fraction(value);require(-1<=result<=1,'day contrast outside its bounded range');return result


def rational(value):return {'numerator':value.numerator,'denominator':value.denominator,'value':float(value)}


def bernoulli_kl(q,p):
    if q==0:return -math.log1p(-p)
    if q==1:return -math.log(p)
    return q*math.log(q/p)+(1-q)*math.log((1-q)/(1-p))


def independent_sign_upper(count,n,alpha):
    """Invert the Chernoff lower-tail bound for independent Bernoulli indicators.

    Indicators need not have a common success probability. Their average
    probability is bounded. This is a one-sided fixed-sample bound, not a
    confidence sequence or an exact Clopper-Pearson interval.
    """
    require(type(n) is int and n>0 and type(count) is int and 0<=count<=n and type(alpha) in (int,float) and 0<alpha<1,'invalid sign-bound inputs')
    if count==n:return 1.
    if count==0:return -math.expm1(math.log(alpha)/n)
    q=count/n;low=q;high=1.;target=-math.log(alpha)/n
    for _ in range(100):
        mid=(low+high)/2
        if mid==low or mid==high:break
        if bernoulli_kl(q,mid)<target:low=mid
        else:high=mid
    return high


def analyze_calendar(dates,applications,components,*,confidence=.95):
    """Each planned app/day contributes equally, including unobserved components.

    components[(UTC-date, application)] is an exact paired score difference or
    None. No value is imputed for an invalid/unavailable application panel.
    This function does not verify acquisition provenance; its caller must replay
    the paired source/semantic/resource audits before supplying a score.
    """
    require(isinstance(dates,list) and 2<=len(dates)<=60 and dates==sorted(set(dates)) and all(date.fromisoformat(d).isoformat()==d for d in dates),'fixed distinct real-date calendar required')
    require(isinstance(applications,list) and applications and len(set(applications))==len(applications),'distinct application identities required')
    require(type(confidence) in (int,float) and 0<confidence<1,'confidence outside zero/one')
    expected={(d,a) for d in dates for a in applications};require(set(components)<=expected,'unknown or unplanned calendar component')
    n=len(dates);a=len(applications);days=[];complete=[]
    for day in dates:
        known=[exact(components[day,app]) for app in applications if components.get((day,app)) is not None];missing=a-len(known);total=sum(known,Fraction())
        lower=(total-missing)/a;upper=(total+missing)/a
        days.append({'collection_date_utc':day,'observed_applications':len(known),'missing_applications':missing,'lower':rational(lower),'upper':rational(upper)})
        if not missing:complete.append(lower)
    lower=sum((Fraction(r['lower']['numerator'],r['lower']['denominator']) for r in days),Fraction())/n
    upper=sum((Fraction(r['upper']['numerator'],r['upper']['denominator']) for r in days),Fraction())/n
    alpha=1-confidence;radius=math.sqrt(2*math.log(2/alpha)/n)
    possible_positive=sum(r['upper']['numerator']>0 for r in days);possible_negative=sum(r['lower']['numerator']<0 for r in days)
    t={'status':'unavailable','reason':'The full planned calendar is not observed or its size has no prespecified t critical value.'}
    if len(complete)==n and n in T95:
        values=[float(v) for v in complete];sd=statistics.stdev(values)
        if sd==0:t={'status':'unavailable','reason':'Observed day-level variance is zero; no point-width population t interval is reported.','observed_sd':0.}
        else:
            mean=statistics.mean(values);half=T95[n]*sd/math.sqrt(n)
            t={'status':'reported','confidence':.95,'mean':mean,'sample_sd':sd,'degrees_of_freedom':n-1,'critical_value_rounded':T95[n],
                'lower':max(-1.,mean-half),'upper':min(1.,mean+half),'assumptions':'Independent approximately normally distributed day-level contrasts, or an adequate day-level approximation; no adaptive stopping.'}
    # The t table is only a two-sided 95% table; other requested levels cannot use it.
    if confidence!=.95:t={'status':'unavailable','reason':'The prespecified t table contains only two-sided 95% critical values.'}
    return {'planned_days':n,'applications_per_day':a,'complete_days':len(complete),'missing_application_days':sum(r['missing_applications'] for r in days),
        'independent_unit':'UTC collection-day aggregate','days':days,
        'finite_campaign_identification':{'lower':rational(lower),'upper':rational(upper),'point_identified':lower==upper,
            'meaning':'Equal-weight realized planned-calendar contrast; missing app/day components range over [-1,1]. This is not a confidence interval.'},
        'paired_day_t_interval':t,
        'bounded_independent_day_sensitivity':{'confidence':confidence,'lower':max(-1.,float(lower)-radius),'upper':min(1.,float(upper)+radius),'hoeffding_radius':radius,
            'assumptions':'Mutually independent bounded day contrasts; means may differ by date. Target is the average of planned-day expectations, not arbitrary future applications.'},
        'sign_incidence_sensitivity':{'confidence':confidence,'possible_positive_days':possible_positive,'possible_negative_days':possible_negative,
            'lower':-independent_sign_upper(possible_negative,n,alpha/2),'upper':independent_sign_upper(possible_positive,n,alpha/2),
            'assumptions':'Mutually independent day contrasts in [-1,1]. Unknown signs are counted in each possible direction; two one-sided bounds use alpha/2. No identical-distribution assumption.'},
        'stopping':'fixed_days_no_effect_based_extension','within_day_operations_treated_as_independent':False}
