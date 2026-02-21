"""
Database Migration Script
Adds intent_score column to existing Lead table
Run this once to update the database schema
"""

from app import app, db, Lead
from sqlalchemy import text

print("=" * 60)
print("🔄 DATABASE MIGRATION - Adding intent_score column")
print("=" * 60)
print()

with app.app_context():
    try:
        # Check if column already exists
        result = db.session.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='lead' AND column_name='intent_score';
        """))
        
        if result.fetchone():
            print("✅ Column 'intent_score' already exists. No migration needed.")
        else:
            print("📝 Adding 'intent_score' column to Lead table...")
            
            # Add the column with default value
            db.session.execute(text("""
                ALTER TABLE lead 
                ADD COLUMN intent_score INTEGER DEFAULT 1;
            """))
            
            db.session.commit()
            
            print("✅ Column 'intent_score' added successfully!")
            print()
            print("📊 Updating existing leads with default score...")
            
            # Update existing leads to have score of 3 (medium quality)
            db.session.execute(text("""
                UPDATE lead 
                SET intent_score = 3 
                WHERE intent_score IS NULL;
            """))
            
            db.session.commit()
            
            print("✅ Existing leads updated with default score: 3/5")
        
        print()
        print("=" * 60)
        print("🎉 MIGRATION COMPLETE!")
        print("=" * 60)
        print()
        
        # Verify the change
        result = db.session.execute(text("SELECT COUNT(*) FROM lead;"))
        count = result.scalar()
        print(f"📊 Total leads in database: {count}")
        
    except Exception as e:
        print(f"❌ MIGRATION FAILED: {e}")
        print()
        print("If you see 'column already exists', that's OK - no action needed.")
        db.session.rollback()