"""Conditional precision scenarios; no main sample-size recommendation."""
import math

# NIST two-sided 95% t table (0.975 column), printed to three decimals.
T95={12:2.201,20:2.093,30:2.045,40:2.023,60:2.001}
SOURCE='https://www.itl.nist.gov/div898/handbook/eda/section3/eda3672.htm'


def planning_table():
    return [{'independent_day_blocks':n,'assumed_day_contrast_sd':sd,'t95_rounded':critical,
             'conditional_half_width':critical*sd/math.sqrt(n),'assumption':'independent approximately normal day-level paired score differences; SD supplied as scenario, not estimated'}
            for n,critical in T95.items() for sd in (.05,.10,.20,.30,.50)]


def iid_zero_failure_illustration(n,confidence=.95):
    if type(n) is not int or n<=0 or not 0<confidence<1:raise ValueError('positive trials and confidence in zero/one required')
    return {'iid_trials':n,'one_sided_lower_bound':(1-confidence)**(1/n),
            'assumption':'independent identically distributed Bernoulli trials, all successful; not applicable as certification for clustered browser probes'}


def iid_zero_failure_required(target=.99,confidence=.95):
    if not 0<target<1 or not 0<confidence<1:raise ValueError('target/confidence outside zero/one')
    return math.ceil(math.log1p(-confidence)/math.log(target))
