code will be in python

I want to build a trading system working in bybit. I have an account and a subaccount is ready to trade. (api_key,secret: design\_cred_bybit_MT325748552))
It will basically not keep trading all the time, but wait for the right signal that can create profit
- I will open postions based on the news of new listing from Upbit in Korea. (api_key,secret is in design/_cred_upbit)
a. new scrapping will scrap news every hour. 
b. when a new listing symbol exists in bybit usdt-perpetual market like upbit symbol+USDT, it will open the position of it before the time of listing
