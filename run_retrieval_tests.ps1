# LegalMind Retrieval Verification Test Script
# Runs all 6 test queries and saves the response to backups/retrieval_test_results.json

Write-Host "Starting LegalMind 6-Query Retrieval Test..." -ForegroundColor Cyan
.\.venv\Scripts\python -m scripts.run_retrieval_tests --output backups/retrieval_test_results.json

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nTest complete! Results saved to: backups/retrieval_test_results.json" -ForegroundColor Green
} else {
    Write-Host "`nTest encountered an error." -ForegroundColor Red
}

