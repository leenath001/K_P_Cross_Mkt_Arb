# K_P_Cross_Mkt_Arb

## Idea: 
- Grab odds for an event from pinnacle 
    - Identify implied odds of each outcome, taking into acc VIG (function done)
- Find odds for similar event on Kalshi 
    - Trade limit (resting order) if kalshi odds < implied odds 

## Next Steps: 
- Now that we have the DF, save it as a csv and have trading module handle 
    - Want: Place resting order, time it, if not filled within 5m cancel. 
    - Constantly ping Pinacle to see if signal is stil good. 

## Notes: 
- See if inclusion of other books makes the edge better