import { PlaywrightCrawler } from 'crawlee';
import { writeFileSync } from 'fs';
import "dotenv/config"; 

const startUrls = ['https://sat.aljazeera.net/en'];  // fixed seed
const visited = new Set();

const crawler = new PlaywrightCrawler({
    maxRequestsPerCrawl: 100,  // adjust as needed
    async requestHandler({ request, page, enqueueLinks, log }) {
        visited.add(request.url.split('#')[0]);
        log.info(`Crawled: ${request.url}`);
        await enqueueLinks({ selector: 'a' });
    },
    headless:false ,
});

await crawler.run(startUrls);

// Write unique URLs to urls.txt
const urls = [...visited].sort();
writeFileSync('urls.txt', urls.join('\n'));
console.log(`Saved ${urls.length} URLs to urls.txt`);