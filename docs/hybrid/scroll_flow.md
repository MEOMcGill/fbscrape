# Scroll Request Flow — Capture Analysis

Goal: characterize what requests fire each time `ProfileCometTimelineFeedRefetchQuery` (the pagination GraphQL) is dispatched during a scrolling profile-timeline scrape. Each anchor = one scroll-triggered pagination.

**Method:** for every `ProfileCometTimelineFeedRefetchQuery` request in the capture, collect all other captured requests within `[-2s, +5s]` of its timestamp. Aggregate across all anchors to find recurring vs. one-off neighbors.

**Sessions analyzed:** the 3 longest captures from `data/hybrid/batch_20260429T202827Z/`.

- `network_20260429T210202Z__12192636156.jsonl` — 5114 records, 187 pagination anchors
- `network_20260429T210652Z__15126126036.jsonl` — 2699 records, 115 pagination anchors
- `network_20260429T210904Z__16156262335.jsonl` — 1671 records, 60 pagination anchors

---

## Representative scroll burst (relative to anchor t=0)

From `network_20260429T210202Z__12192636156.jsonl` — sample anchor windows from mid-session.

### Anchor example 1
```
     time  label                                                                 
---------  ----------------------------------------------------------------------
  - 1.25s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQPbhaWIZ-5gMuIdwhNASyi7DnOfwt3vVt2fySu60-a2NkR1jVNRN2mrUUhykHYSD_WQcXFdR4BTIQb2ksr9wSAI.mp4
  - 1.08s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPLJK37DWaQx5x8Q0jDCA3gZX_kKNXJf3km-x0z19GhMoqRoZiDXxESQM3noH3_YdGgneFygQZCCMPwu2BF4xyq.mp4
  - 1.03s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPLJK37DWaQx5x8Q0jDCA3gZX_kKNXJf3km-x0z19GhMoqRoZiDXxESQM3noH3_YdGgneFygQZCCMPwu2BF4xyq.mp4
  - 0.99s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPLJK37DWaQx5x8Q0jDCA3gZX_kKNXJf3km-x0z19GhMoqRoZiDXxESQM3noH3_YdGgneFygQZCCMPwu2BF4xyq.mp4
  - 0.79s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  - 0.75s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQMptidLam7PYnJ8HLBha3Dwf9Xaerku5T5aSobSpvOdCqiN2l13IHKDXSdOwjgJ4ZU3gWMPVDIk0Le3C8FhpV4O8GsrywNA2X7K3lo.mp4
  - 0.51s  xhr:www.facebook.com/ajax/bulk-route-definitions/                     
  - 0.34s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQMptidLam7PYnJ8HLBha3Dwf9Xaerku5T5aSobSpvOdCqiN2l13IHKDXSdOwjgJ4ZU3gWMPVDIk0Le3C8FhpV4O8GsrywNA2X7K3lo.mp4
  - 0.32s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/541902926_10162191290941491_8608598971636338995_n.jpg
  - 0.32s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  - 0.31s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  - 0.19s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.71878-15/490356603_1123381729828685_5155128820889943007_n.jpg
  - 0.19s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.71878-15/490356603_1123381729828685_5155128820889943007_n.jpg
  + 0.00s  GQL ProfileCometTimelineFeedRefetchQuery                                <== ANCHOR
  + 0.02s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 0.07s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 0.19s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 0.19s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 0.32s  xhr:www.facebook.com/ajax/bulk-route-definitions/                     
  + 0.33s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.75761-15/487939598_18501485674027365_159516651815138113_n.jpg
  + 0.33s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/515505664_24227758866836688_8855862477237732209_n.jpg
  + 0.38s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.75761-15/488543114_18501485734027365_8777699150927125611_n.jpg
  + 0.39s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.75761-15/489850816_18501485749027365_9051010010687537613_n.jpg
  + 0.41s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.75761-15/489275820_18501485800027365_1228999224264181069_n.jpg
  + 0.44s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.75761-15/489259441_18501485716027365_4801623464018785749_n.jpg
  + 2.29s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 2.29s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  + 2.76s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 2.77s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  + 2.81s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  + 2.90s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOgsUjTzUwAhOPVMNIODgO0MLFPd6hV9UG_HlvFistXsYlTTuWwKTfZELcSVpcvkizvR1_7Sk4mmGLVsV4OLr8E1BYQCq3185dG2XMKWXQBFQ.mp4
  + 2.90s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 2.90s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
  + 2.90s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
  + 2.90s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/641790948_1476251690529345_537660000661775816_n.jpg
  + 2.92s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOgsUjTzUwAhOPVMNIODgO0MLFPd6hV9UG_HlvFistXsYlTTuWwKTfZELcSVpcvkizvR1_7Sk4mmGLVsV4OLr8E1BYQCq3185dG2XMKWXQBFQ.mp4
  + 2.95s  GQL ProfileCometTimelineFeedRefetchQuery                              
  + 2.96s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/666010268_27538354339097656_3870252918672222146_n.jpg
  + 3.06s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489875909_1308052376935113_5838801599901409145_n.jpg
  + 3.15s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489952426_1072285124723982_1884392283486704997_n.jpg
  + 3.15s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/530385702_10239368463488870_639073215434124292_n.jpg
  + 3.15s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/468208964_10161854458609383_9155771073237457704_n.jpg
  + 3.15s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489875909_1308052376935113_5838801599901409145_n.jpg
  + 3.22s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
```

### Anchor example 2
```
     time  label                                                                 
---------  ----------------------------------------------------------------------
  - 0.66s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  - 0.66s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  - 0.18s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  - 0.18s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m367/AQP1AkbyQhtwf-tATpUAasIUTQj_RFhbWjl_hKGBlxlGirDp1HSpwUoomhahtuFghE8plepMzheTBUKJ2S0N-7wwIUST_BAtqP06cpc.mp4
  - 0.14s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQNRso6tr3FTrRM1l_h4gJIK0vl34bEOqigsFDHXbzw8Bzo_wVulNVYuya6oaEqkf3e3kvFobulbajIOEMT7QNU.mp4
  - 0.05s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOgsUjTzUwAhOPVMNIODgO0MLFPd6hV9UG_HlvFistXsYlTTuWwKTfZELcSVpcvkizvR1_7Sk4mmGLVsV4OLr8E1BYQCq3185dG2XMKWXQBFQ.mp4
  - 0.05s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  - 0.05s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
  - 0.05s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
  - 0.05s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/641790948_1476251690529345_537660000661775816_n.jpg
  - 0.02s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOgsUjTzUwAhOPVMNIODgO0MLFPd6hV9UG_HlvFistXsYlTTuWwKTfZELcSVpcvkizvR1_7Sk4mmGLVsV4OLr8E1BYQCq3185dG2XMKWXQBFQ.mp4
  + 0.00s  GQL ProfileCometTimelineFeedRefetchQuery                                <== ANCHOR
  + 0.01s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/666010268_27538354339097656_3870252918672222146_n.jpg
  + 0.11s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489875909_1308052376935113_5838801599901409145_n.jpg
  + 0.21s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489952426_1072285124723982_1884392283486704997_n.jpg
  + 0.21s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/530385702_10239368463488870_639073215434124292_n.jpg
  + 0.21s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/468208964_10161854458609383_9155771073237457704_n.jpg
  + 0.21s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489875909_1308052376935113_5838801599901409145_n.jpg
  + 0.27s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOXrnnXtZKfofRv19B1F2WPqT1LfTHQghUrGsgzN3Zm05p-HjOSOiGVqubRWXtwJeRUWjxPQ_orlr5FHv2qrdem.mp4
  + 4.42s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 4.64s  GQL ProfileCometTimelineFeedRefetchQuery                              
  + 4.76s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/473537905_3902825139983320_1588644409233795482_n.jpg
  + 4.77s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 4.78s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 4.80s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489197382_1071880204764474_326615334058909356_n.jpg
```

### Anchor example 3
```
     time  label                                                                 
---------  ----------------------------------------------------------------------
  - 0.23s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 0.00s  GQL ProfileCometTimelineFeedRefetchQuery                                <== ANCHOR
  + 0.11s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/473537905_3902825139983320_1588644409233795482_n.jpg
  + 0.12s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 0.13s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 0.16s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489197382_1071880204764474_326615334058909356_n.jpg
  + 0.54s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489298785_1071880201431141_8099532788131197934_n.jpg
  + 0.54s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 0.54s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 0.62s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.71878-15/489860668_1397799484894436_2476798285597874348_n.jpg
  + 0.62s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.71878-15/489860668_1397799484894436_2476798285597874348_n.jpg
  + 0.64s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 0.80s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 0.82s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/616989612_1430962578418274_364224783209955781_n.jpg
  + 0.96s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t1.6435-1/178845543_10158878752420199_8891921882223967865_n.jpg
  + 0.96s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489304167_1211124327068768_3316026752900022575_n.jpg
  + 0.98s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489121168_1211125193735348_6977773541158895326_n.jpg
  + 0.99s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/489458043_1211125163735351_2633323792570748448_n.jpg
  + 1.00s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-6/488909425_1211125170402017_4372250356338142664_n.jpg
  + 1.07s  GQL ProfileCometTimelineFeedRefetchQuery                              
  + 1.09s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/641790948_1476251690529345_537660000661775816_n.jpg
  + 1.28s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQPeXpQoSCNkXu7b2JCKimR9X6XcaSLPrWfq7cIXdbTiEsxGpLezbW4bl0jcx6y29gqWpDk_pGYvrTV2YlcJNrI7.mp4
  + 1.28s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQPeXpQoSCNkXu7b2JCKimR9X6XcaSLPrWfq7cIXdbTiEsxGpLezbW4bl0jcx6y29gqWpDk_pGYvrTV2YlcJNrI7.mp4
  + 1.31s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOQj4ICiiF2jwEJFqHAeBusGoZzUzOfZ93ZPfNd2L4ZMwAuORYQQEvx_FoHSBOjVUqst9bQ-03RZQwnq0SpdmMrMFw1PBXz0TT8lZJDV4dd0A.mp4
  + 1.53s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQOQj4ICiiF2jwEJFqHAeBusGoZzUzOfZ93ZPfNd2L4ZMwAuORYQQEvx_FoHSBOjVUqst9bQ-03RZQwnq0SpdmMrMFw1PBXz0TT8lZJDV4dd0A.mp4
  + 3.08s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 3.08s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 3.16s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 3.16s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 3.18s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/480704404_28580134378299501_2420064791499356038_n.jpg
  + 3.24s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 3.28s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQPeXpQoSCNkXu7b2JCKimR9X6XcaSLPrWfq7cIXdbTiEsxGpLezbW4bl0jcx6y29gqWpDk_pGYvrTV2YlcJNrI7.mp4
  + 3.80s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/487225433_521007827516270_1090016305857487597_n.jpg
  + 3.80s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 3.80s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/487225433_521007827516270_1090016305857487597_n.jpg
  + 3.80s  xhr:www.facebook.com/video/unified_cvc/                               
  + 3.86s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQOyGUfl-RQTkShr-f3Ys59LQ733Uvrrp6qxXlIAHwcx5P0MRnWXf1qOxV1CfpoCylhWD_dUHD50c1IurUmuTcc.mp4
  + 3.87s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m86/AQPna4HGslJeOS2laCfkRjNib5ywqT2hF6leCR85ZPFsVdwbc5Az9EeTzk_tJEOs6BYWQKZwhbDsJzhOm9e0oWaAJJzacmei3TElmDg.mp4
  + 4.02s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQO7lxbtd_Mv2X1RbEhW-s4Je_yL1T5JRxbrxLJYge9tn1NGyFk0eHKFTZaakSDy_zfnn-S_w3hMI8xCDZb2okW4FdOVl5X4rdBc_Zs.mp4
  + 4.02s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQNzfEaxIeKxy6rtJTtP5BlrWBo5KPJRMozRfXr4uy6LU4vuu1WxM9a7Y7_wjd4-ouqGZAoS9ZtdHHOlH9XjuIYy.mp4
  + 4.03s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQNzfEaxIeKxy6rtJTtP5BlrWBo5KPJRMozRfXr4uy6LU4vuu1WxM9a7Y7_wjd4-ouqGZAoS9ZtdHHOlH9XjuIYy.mp4
  + 4.04s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQO7lxbtd_Mv2X1RbEhW-s4Je_yL1T5JRxbrxLJYge9tn1NGyFk0eHKFTZaakSDy_zfnn-S_w3hMI8xCDZb2okW4FdOVl5X4rdBc_Zs.mp4
  + 4.12s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/641790948_1476251690529345_537660000661775816_n.jpg
  + 4.12s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
  + 4.28s  GQL ProfileCometTimelineFeedRefetchQuery                              
  + 4.41s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/375814722_700457198787983_6585708359171621860_n.jpg
  + 4.42s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/489959282_10234966903010039_7810946997477523458_n.jpg
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQNuKyBnTjSztyCHnvWjvGiddlfZ38V8eynP3TeC71g8dcH10aNpyrsiC3jnH2MIqiuug2DvmzSL-t3eB_utFJbtK_gJ5thpMdo4Eyx8Bc2bsQ.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQNuKyBnTjSztyCHnvWjvGiddlfZ38V8eynP3TeC71g8dcH10aNpyrsiC3jnH2MIqiuug2DvmzSL-t3eB_utFJbtK_gJ5thpMdo4Eyx8Bc2bsQ.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQMsfX-8_rG3s3hEM-uEaFEcVf89yOOEc6oM0RKKz6wBv0jQLPz-SDVdj8ShiYWyS4xxqjnwJNB8EeVYO17jr8gv.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQMsfX-8_rG3s3hEM-uEaFEcVf89yOOEc6oM0RKKz6wBv0jQLPz-SDVdj8ShiYWyS4xxqjnwJNB8EeVYO17jr8gv.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQN9JnRyylzJprvfISldNkyTiNq3r0X3vgeB60ByLtkOV3lJ-C78zwSEinA1j0LQY7fQ5aYrOjjRmAaPfqlDc2N_kR29qrz_5phkWyZw4GkE2g.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOuqJdoAozZQZcZjPrkzuF5En6Nd6s2milKbxW2EDH4RP7i8KTuURdUiaPTjXFVuivyEfHyVHpBeahiMDLRa2iY.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOuqJdoAozZQZcZjPrkzuF5En6Nd6s2milKbxW2EDH4RP7i8KTuURdUiaPTjXFVuivyEfHyVHpBeahiMDLRa2iY.mp4
  + 4.42s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQMsfX-8_rG3s3hEM-uEaFEcVf89yOOEc6oM0RKKz6wBv0jQLPz-SDVdj8ShiYWyS4xxqjnwJNB8EeVYO17jr8gv.mp4
  + 4.42s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t1.6435-1/178845543_10158878752420199_8891921882223967865_n.jpg
  + 4.42s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/461927229_8490719474299542_8594037478390586735_n.jpg
  + 4.53s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m69/AQOuqJdoAozZQZcZjPrkzuF5En6Nd6s2milKbxW2EDH4RP7i8KTuURdUiaPTjXFVuivyEfHyVHpBeahiMDLRa2iY.mp4
  + 4.67s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489587749_1364598414663037_8651569816015930041_n.jpg
  + 4.67s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489587749_1364598414663037_8651569816015930041_n.jpg
  + 4.77s  fetch:video.fyhu1-1.fna.fbcdn.net/o1/v/t2/f2/m366/AQN9JnRyylzJprvfISldNkyTiNq3r0X3vgeB60ByLtkOV3lJ-C78zwSEinA1j0LQY7fQ5aYrOjjRmAaPfqlDc2N_kR29qrz_5phkWyZw4GkE2g.mp4
  + 4.83s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489221957_1980416256066996_4814897975844184550_n.jpg
  + 4.84s  image:scontent.fyhu1-1.fna.fbcdn.net/v/t15.5256-10/489221957_1980416256066996_4814897975844184550_n.jpg
```

## Per-scroll recurring requests (aggregated across all sessions)

Aggregated across **362** anchor windows total.

| Request | Avg per anchor | Coverage* | Median first dt | Notes |
|---|---:|---:|---:|---|
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg` | 1.00 | 51% | -0.23s | |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/244034653_5195232997177077_8404466875404549656_n.jpg` | 0.47 | 31% | -0.19s | |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/634584008_122095854627279670_9194904432171727349_n.jpg` | 0.86 | 30% | +0.10s | |
| `xhr:www.facebook.com/video/unified_cvc/` | 0.31 | 26% | +0.16s | |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/375757131_888161929369628_7113429951280573635_n.jpg` | 0.34 | 16% | -0.04s | |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/633741803_122096894265267978_4218589471053689129_n.jpg` | 0.49 | 16% | +0.29s | |
| `xhr:www.facebook.com/ajax/bulk-route-definitions/` | 0.31 | 10% | -0.11s | |

\* Coverage = fraction of anchor windows that contain at least one of these requests.

## Sporadic / engagement-driven (appear in some windows but not most)

| Request | Avg per anchor | Coverage |
|---|---:|---:|
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/641790948_1476251690529345_537660000661775816_n.jpg` | 0.45 | 37% |
| `xhr:www.facebook.com/ajax/bulk-route-definitions/` | 1.46 | 35% |
| `xhr:www.facebook.com/video/unified_cvc/` | 0.21 | 18% |
| `script:static.xx.fbcdn.net/rsrc.php (static rsrc)` | 0.82 | 15% |
| `image:static.xx.fbcdn.net/rsrc.php (static rsrc)` | 0.14 | 10% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t1.6435-1/82577806_102965687908319_1023468329857187840_n.jpg` | 0.07 | 6% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/311801286_6212708505413246_2951196063844590281_n.jpg` | 0.09 | 6% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/375814722_700457198787983_6585708359171621860_n.jpg` | 0.06 | 6% |
| `xhr:www.facebook.com/ajax/bnzai` | 0.04 | 4% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t1.30497-1/453178253_471506465671661_2781666950760530985_n.png` | 0.04 | 4% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/492084381_10163251371464529_8326220351696703170_n.jpg` | 0.04 | 4% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/461927229_8490719474299542_8594037478390586735_n.jpg` | 0.03 | 3% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/361597813_2012327382436152_5001268887296633504_n.jpg` | 0.03 | 3% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/468997474_10160635654531767_2511003117376943309_n.jpg` | 0.03 | 3% |
| `xhr:www.facebook.com/ajax/bootloader-endpoint/` | 0.06 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/480704404_28580134378299501_2420064791499356038_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/458302316_10220937161097575_5992029381259392782_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/453744172_3819178954979197_532170972329784066_n.jpg` | 0.03 | 2% |
| `xhr:www.fbsbx.com/ajax/bootloader-endpoint/` | 0.02 | 2% |
| `image:static.xx.fbcdn.net/images/emoji.php/v9/t9/2/16/1f1e8_1f1e6.png` | 0.02 | 2% |
| `image:static.xx.fbcdn.net/images/emoji.php/v9/te4/2/16/1f6a8.png` | 0.03 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/467444746_3045017562316804_5053923227509598097_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/473537905_3902825139983320_1588644409233795482_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/461788339_10163166835683455_7330957268577322915_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t1.6435-1/129611720_2779293912389342_6933770692233242076_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/662530474_1335543731734018_3675628031719004095_n.jpg` | 0.02 | 2% |
| `image:static.xx.fbcdn.net/images/emoji.php/v9/ted/2/16/2764.png` | 0.02 | 2% |
| `image:static.xx.fbcdn.net/images/emoji.php/v9/teb/2/16/1f642.png` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/494630831_10163274170626255_8041847969180383957_n.jpg` | 0.02 | 2% |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/412974024_122099547464162204_7406822071869322320_n.jpg` | 0.02 | 2% |
| ... (3150 more truncated) | | |

## Page-load only (fire before first pagination, then never)

| Request | Total occurrences before first anchor |
|---|---:|
| `xhr:www.facebook.com/ajax/bootloader-endpoint/` | 13 |
| `xhr:www.facebook.com/ajax/route-definition/` | 5 |
| `stylesheet:static.xx.fbcdn.net/rsrc.php (static rsrc)` | 5 |
| `beacon:www.facebook.com/ajax/bnzai` | 3 |
| `image:scontent.fyhu1-1.fna.fbcdn.net/v/t51.82787-15/681861609_18587375497027365_8406222411295524173_n.jpg` | 3 |
| `GQL FBYRPTimeLimitsEnforcementQuery` | 3 |
| `GQL fetchMWChatVideoAutoplaySettingQuery` | 3 |
| `document:www.fbsbx.com/maw_proxy_page/` | 3 |
| `image:scontent.xx.fbcdn.net/hads-ak-prn2/1487645_6012475414660_1439393861_n.png` | 3 |
| `GQL CometNotificationsDropdownQuery` | 3 |
| `fetch:static.xx.fbcdn.net/rsrc.php (static rsrc)` | 3 |
| `xhr:static.xx.fbcdn.net/btmanifest` | 3 |
| `GQL usePseudoBlockedUserInterstitialF3Query` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An_NYk94x95Kypus5FJQ4k1oKEtko7-rUveEhwXytks318UoxoopBczHucoGDTjX4YcuAmIDkUw9SWBKhz7XYoAsQO_zxmWdkseO4YNdrvYmnIdmeAk.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An9Tph7xL5aYi3BTTpVl2ow-l1vEv0-qYrBxereq6t6fWgcNnxeyJLsd3DuGnbCLdLFmcvvxn5ivo85wlrIv-EZhT1EucYZ-qoMW0ahYWHGpBBoqSjw.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An8KSKhioMIUVSZ4NYh8JnAchGmGQVDA71Hulkn5JQZsO-ejSYcV2dWPMQo-ZhSa71MfFqgrDHlCASWFxgbtV3pYQqVdSus0zicQO6_n.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An9yyyXsW_c8BzV1_m0x9WqIPc7IKsshuStlldoUJeshdwMKRkmQmdPEGGwMopV9sH85YDKihkJ8DjNhx1wb2IaUuEOGzE_ji5F9MEXs_50bbA-IXQ.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An8UpDtYTxwRfyFUAQkYpPLVzCcFZAyT58b4mM9QRn-9IZWdHfcWsX1lOuHAsupZu8HCFEX8uobe6VdR6H9B8v2Z_FzSMfB8MYN36w.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An9Ob_EAguyCtqplSNOuikWQ6JjlxIh8vYoEgMXvR0CwneLvNdYwV-1NZgwJfR4aZ4QXhIpQUZxunwGdGubGw20jp-9DikfLbRVh-i4tobncZg9LGpE.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An-iEpi04A6D-3XVq3wsUCtKdkB4w6IdurXeouCv4FsHYljtXw-fLrmbfC8B8zH5fPrckxLdEzR7V7dfidlNcrNoGsJWxiPDnzyNK-npl_KrJieMlg.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An9UFu6eXdDopP-SnPAhX-9SngOH_VeoYgHIGK6re6qbARX_PMcNl8Z14QPPFnVzbcfOQwxJxX5q-H5Fy3602EbOrVHhr1m4VSMXypn3fj2NSJongRg.kf` | 3 |
| `xhr:scontent.fyhu1-1.fna.fbcdn.net/m1/v/t6/An919EwtdC17ceBi2men2TIR0UhNUwitUvpE61_Jbs44ggFt3mvLWkKiHP_CSmw72ohKdyEscSIBof6y1FeS57I3kGfh5tg96jndT5mnnEuJlpXuvuUj.kf` | 3 |
| `xhr:static.xx.fbcdn.net/rsrc.php (static rsrc)` | 3 |
| `websocket:gateway.facebook.com/ws/lightspeed` | 3 |
| `GQL OhaiWebClientMessengerConfigsQuery` | 3 |
| `GQL CometSearchBootstrapKeywordsDataSourceQuery` | 3 |
| `GQL useMWEncryptedBackupsFetchBackupIdsV2Query` | 3 |
| `GQL RTWebCallBlockSettingHooksQuery` | 3 |
| `GQL MAWVerifyThreadCutover_ContactCapabilities2Query` | 3 |
| `script:www.facebook.com/static_resources/webworker/init_script/` | 3 |
| ... (53 more truncated) | |

## Focus endpoints

These were called out in the investigation as candidate ambient/telemetry endpoints that pure GraphQL replay would miss.

| Endpoint | Per-anchor avg | Coverage | Class |
|---|---:|---:|---|
| `xhr:www.facebook.com/ajax/bulk-route-definitions/` | 0.31 | 10% | per-scroll |
| `xhr:www.facebook.com/ajax/bulk-route-definitions/` | 1.46 | 35% | sporadic |
| `xhr:www.facebook.com/ajax/bnzai` | 0.04 | 4% | sporadic |
| `beacon:www.facebook.com/ajax/bnzai` | 0.00 | 0% | page-load only |
| `xhr:www.facebook.com/ajax/bootloader-endpoint/` | 0.06 | 2% | sporadic |
| `xhr:www.fbsbx.com/ajax/bootloader-endpoint/` | 0.02 | 2% | sporadic |
| `xhr:www.facebook.com/ajax/bootloader-endpoint/` | 0.03 | 1% | page-load only |
| `xhr:www.facebook.com/ajax/relay-ef/` | 0.01 | 1% | sporadic |
| `xhr:www.facebook.com/ajax/relay-ef/` | 0.00 | 0% | page-load only |
| `xhr:www.facebook.com/ajax/route-definition/` | 0.00 | 0% | page-load only |

## Typical ordering within a scroll burst

Median first-occurrence dt (relative to anchor at t=0) for top per-scroll requests:

```
      dt   coverage  label
--------  ---------  ------------------------------------------------------------
   -0.23s       51%   image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/260306860_329011009051401_3999712331402185413_n.jpg
   -0.19s       31%   image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/244034653_5195232997177077_8404466875404549656_n.jpg
   -0.11s       10%   xhr:www.facebook.com/ajax/bulk-route-definitions/
   -0.04s       16%   image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/375757131_888161929369628_7113429951280573635_n.jpg
   +0.10s       30%   image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/634584008_122095854627279670_9194904432171727349_n.jpg
   +0.16s       26%   xhr:www.facebook.com/video/unified_cvc/
   +0.29s       16%   image:scontent.fyhu1-1.fna.fbcdn.net/v/t39.30808-1/633741803_122096894265267978_4218589471053689129_n.jpg
```

## Implications for Path B

Path B-lite (replay `ProfileCometTimelineFeedRefetchQuery` from inside the live page without scrolling) would NOT fire any requests classified as **per-scroll** above. Whether Facebook gates on those is unproven — but the per-scroll list is the concrete inventory of "what real scrolling generates that pure replay would not."

Page-load-only requests aren't a Path-B problem: they fire once when the page boots (which Path B-lite still does via Camoufox), and then never again. 

Sporadic requests are mostly engagement / viewport-driven (image preloads of upcoming posts, video thumbnail fetches, hover-triggered route prefetches). Pure replay would be silent on these too, but they correlate with content rather than with the scroll event itself, so missing them mainly looks like "user isn't actually viewing posts." 

Bulk-route-definitions in particular: see the focus table — its coverage tells us whether it's a tight per-scroll signal or just hover-driven noise.