# Trial court scraper for the los angeles [case record system](https://www.lacourt.ca.gov/). 

Based on my investigation the following can be done:
- Enumerate through predictable case numbers     
- Search for the case number to find documents
- Solve a captcha and get the document link
- download the document
  
Please implement the scraper in the [los_angeles_scraper.py](https://github.com/rreeves8/scraping-assignment/blob/main/src/ports/los_angeles_scraper.py).

You have access to a browser base account that does the following:
- Browser instance in a cloud environment, connected to the internet through a proxy
- Connect and use it through the factory and playwight

The code can be ran using `uv run scraper`
