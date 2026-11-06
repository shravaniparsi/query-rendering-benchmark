from datetime import date,timedelta
from fractions import Fraction
import itertools
import math
import unittest

from experiments.study_design.calendar_analysis import analyze_calendar,independent_sign_upper


class CalendarAnalysisContracts(unittest.TestCase):
    def dates(self,n):return [(date(2027,1,1)+timedelta(days=i)).isoformat() for i in range(n)]

    def test_missing_app_days_do_not_renormalize_an_unfavorable_component_away(self):
        dates=self.dates(2);apps=['A','B'];values={(dates[0],'A'):Fraction(-1,2),(dates[0],'B'):Fraction(1,2),(dates[1],'A'):Fraction(-1,2)}
        actual=analyze_calendar(dates,apps,values);bounds=actual['finite_campaign_identification']
        self.assertEqual((bounds['lower']['numerator'],bounds['lower']['denominator']),(-3,8));self.assertEqual((bounds['upper']['numerator'],bounds['upper']['denominator']),(1,8))
        self.assertEqual(actual['complete_days'],1);self.assertEqual(actual['missing_application_days'],1);self.assertFalse(actual['within_day_operations_treated_as_independent'])
        empty=analyze_calendar(dates,apps,{})['finite_campaign_identification'];self.assertEqual(empty['lower']['value'],-1);self.assertEqual(empty['upper']['value'],1)

    def test_zero_observed_variance_is_not_population_certainty(self):
        dates=self.dates(60);actual=analyze_calendar(dates,['A'],{(d,'A'):Fraction() for d in dates})
        self.assertTrue(actual['finite_campaign_identification']['point_identified']);self.assertEqual(actual['finite_campaign_identification']['lower']['value'],0)
        self.assertEqual(actual['paired_day_t_interval']['status'],'unavailable');sensitivity=actual['sign_incidence_sensitivity']
        expected=1-.025**(1/60);self.assertAlmostEqual(sensitivity['upper'],expected);self.assertAlmostEqual(sensitivity['lower'],-expected)
        self.assertGreater(actual['bounded_independent_day_sensitivity']['hoeffding_radius'],.3)

    def test_small_nonidentical_bernoulli_models_satisfy_the_sign_bound(self):
        # Exhaustive outcome probabilities provide a check independent of the inversion code.
        alpha=.1
        for probabilities in ((.1,.2,.8),(.01,.9,.95),(0.,.5,1.),(.9,.9,.9)):
            mean=sum(probabilities)/len(probabilities);noncoverage=0.
            for bits in itertools.product((0,1),repeat=3):
                probability=math.prod(p if bit else 1-p for p,bit in zip(probabilities,bits))
                if independent_sign_upper(sum(bits),3,alpha)<mean:noncoverage+=probability
            self.assertLessEqual(noncoverage,alpha+1e-12)
        self.assertAlmostEqual(independent_sign_upper(0,60,.05),1-.05**(1/60));self.assertEqual(independent_sign_upper(60,60,.05),1)
        self.assertTrue(all(independent_sign_upper(k,60,.05)<=independent_sign_upper(k+1,60,.05) for k in range(60)))

    def test_day_t_interval_uses_days_and_rejects_extra_dates_or_floating_inputs(self):
        dates=self.dates(60);values={(d,'A'):Fraction(1 if i%2 else -1,10) for i,d in enumerate(dates)};actual=analyze_calendar(dates,['A'],values)
        interval=actual['paired_day_t_interval'];self.assertEqual(interval['degrees_of_freedom'],59);self.assertEqual(interval['critical_value_rounded'],2.001);self.assertAlmostEqual(interval['mean'],0)
        for bad in ({**values,('2030-01-01','A'):Fraction()},{**values,(dates[0],'A'):.1}):
            with self.assertRaises(ValueError):analyze_calendar(dates,['A'],bad)


if __name__=='__main__':unittest.main()
