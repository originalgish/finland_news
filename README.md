# Finland News

A self-hosted, single-user news aggregator for Finnish outlets.

It pulls the RSS feeds of five Finnish outlets and removes duplicate articles. Articles about the same event are grouped into one story. Claude translates each article into English, tags it, and writes a headline for each story. You browse the stories through saved views, search them, and read a daily digest of the most important ones.

Only titles, short summaries and links are stored, never full article text. The worker and the web app share nothing but the database. The web app has no login of its own, so it runs behind basic auth.

The design and build order are in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Deployment is in [DEPLOY.md](DEPLOY.md).
