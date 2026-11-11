"""Original qualification caps and the single author-approved history amendment."""
from copy import deepcopy
from datetime import date
from .decision import require

PRIOR_CLAIMS=[
    {'day':'2026-11-07','claim_sha':'96f47c96f80cb9989d1a91c2e099c3e1b34210a8','run_id':'42389365574',
     'source_commit':'d1c627fdb43e8e1564ad32b5d8e7dba02c3b59ab','release_sha256':'ebe26c7b74e24e898e1f0484e4ce4db35f9dc490e03f5f74ff1cae2fb858fbd0'},
    {'day':'2026-11-08','claim_sha':'09f162d6d38fe0e4f6361bf5bd49c75aac86feb0','run_id':'42500110887',
     'source_commit':'f9d4f32f02115f18c14652eba447f28fa2a7fbdb','release_sha256':'33f3db86ce92d432de6299fbec1ab7d2f10df91b97fcf9008422c4c7d1597ca5'},
]
AMENDMENT={'version':'one-extra-qualification-amendment-v1','approval_id':'author-approved-extra-qualification-2026-11-11',
    'previous_limit':2,'total_limit':3,'additional_attempts':1,'prior_claims':PRIOR_CLAIMS}


def amendment(value,campaign):
    require(campaign=='reserved-main-v2' and value==AMENDMENT,'approved amendment must preserve the same campaign, both exact claims and total cap three')
    return deepcopy(value)


def validate_budget(sample):
    limit=sample.get('qualification_attempt_limit')
    if limit is None:
        require('qualification_amendment' not in sample and sample.get('qualification_date_mode')!='complete_profile_and_loaded_full_panel','full qualification requires its bounded attempt history')
        return None
    days=sample['qualification_budget_dates_utc']
    require(type(limit) is int and isinstance(days,list) and days==sorted(set(days))
        and all(isinstance(day,str) and date.fromisoformat(day).isoformat()==day for day in days),'ordered bounded qualification history required')
    if limit==2:
        require(len(days)==2 and 'qualification_amendment' not in sample,'fixed two-date qualification budget required')
    else:
        require(limit==3 and len(days)==3,'only the original two-attempt cap or approved total cap three is permitted')
        amendment(sample.get('qualification_amendment'),sample.get('campaign_id'))
        scheduled=sample.get('qualification_dates_utc')
        require(isinstance(scheduled,list) and len(scheduled)==1 and scheduled[0]>PRIOR_CLAIMS[-1]['day']
            and days==[r['day'] for r in PRIOR_CLAIMS]+scheduled,'exactly one additional date and both original consumed dates required')
        require(sample.get('day_count')==20 and sample.get('qualification_date_mode')=='complete_profile_and_loaded_full_panel','amendment preserves twenty main dates and full qualification')
    return limit
